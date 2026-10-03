#!/usr/bin/env bash
set -u

readonly TAG="remnawave-jobs-watchdog"
readonly STATE_DIR="/var/lib/remnawave-jobs-watchdog"
readonly STATE_FILE="${STATE_DIR}/state"
readonly LOCK_FILE="/run/remnawave-jobs-watchdog.lock"
readonly REQUIRED_STREAK=3
readonly COOLDOWN_SECONDS=600

log() {
    logger -t "$TAG" -- "$*"
}

mkdir -p "$STATE_DIR"
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

streak=0
last_restart=0
if [[ -r "$STATE_FILE" ]]; then
    read -r streak last_restart < "$STATE_FILE" || true
fi
[[ "$streak" =~ ^[0-9]+$ ]] || streak=0
[[ "$last_restart" =~ ^[0-9]+$ ]] || last_restart=0

save_state() {
    local tmp
    tmp="${STATE_FILE}.tmp"
    printf '%s %s\n' "$streak" "$last_restart" > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$STATE_FILE"
}

reset_streak() {
    if (( streak != 0 )); then
        streak=0
        save_state
    fi
}

container_running() {
    [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == "true" ]]
}

redis_int() {
    local value
    value="$(timeout 8 docker exec remnawave-redis valkey-cli -n 1 --raw "$@" 2>/dev/null | tr -d '\r' | tail -n 1)" || return 1
    [[ "$value" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$value"
}

restart_jobs() {
    local reason="$1"
    local now old_pid new_pid
    now="$(date +%s)"

    if (( now - last_restart < COOLDOWN_SECONDS )); then
        log "restart suppressed by cooldown: ${reason}"
        return 0
    fi

    old_pid="$(timeout 8 docker exec remnawave pm2 pid remnawave-jobs 2>/dev/null | tr -d '\r' | tail -n 1)"
    log "restarting remnawave-jobs: ${reason}; old_pid=${old_pid:-unknown}"

    # PM2 6 in this image may return a non-zero code after successfully
    # scheduling the restart, so success is verified by the PID below.
    timeout 30 docker exec remnawave pm2 restart 1 --update-env >/dev/null 2>&1 || true
    sleep 8

    new_pid="$(timeout 8 docker exec remnawave pm2 pid remnawave-jobs 2>/dev/null | tr -d '\r' | tail -n 1)"
    if [[ -n "$new_pid" && "$new_pid" != "0" && "$new_pid" != "$old_pid" ]]; then
        last_restart="$now"
        streak=0
        save_state
        log "remnawave-jobs restarted successfully: new_pid=${new_pid}"
        return 0
    fi

    log "restart verification failed: old_pid=${old_pid:-unknown} new_pid=${new_pid:-unknown}"
    return 1
}

for container in remnawave remnawave-db remnawave-redis; do
    if ! container_running "$container"; then
        reset_streak
        log "skipping: container ${container} is not running"
        exit 0
    fi
done

if ! timeout 8 docker exec remnawave-db sh -c 'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; then
    reset_streak
    log "skipping: PostgreSQL is not ready"
    exit 0
fi

if [[ "$(timeout 8 docker exec remnawave-redis valkey-cli --raw ping 2>/dev/null | tr -d '\r')" != "PONG" ]]; then
    reset_streak
    log "skipping: Valkey is not ready"
    exit 0
fi

jobs_pid="$(timeout 8 docker exec remnawave pm2 pid remnawave-jobs 2>/dev/null | tr -d '\r' | tail -n 1)"
if [[ -z "$jobs_pid" || "$jobs_pid" == "0" ]]; then
    restart_jobs "PM2 reports no running jobs process"
    exit $?
fi

wait_total=0
active_total=0
for queue in \
    NODES_HEALTH_CHECK_QUEUE \
    NODES_RECORD_USER_USAGE_QUEUE \
    NODES_NODE_USERS_QUEUE \
    NODES_START_ALL_NODES_QUEUE
do
    waiting="$(redis_int LLEN "bull:${queue}:wait")" || {
        reset_streak
        log "skipping: cannot read queue ${queue}"
        exit 0
    }
    active="$(redis_int LLEN "bull:${queue}:active")" || {
        reset_streak
        log "skipping: cannot read active jobs for ${queue}"
        exit 0
    }
    wait_total=$((wait_total + waiting))
    active_total=$((active_total + active))
done

if (( wait_total > 0 && active_total == 0 )); then
    streak=$((streak + 1))
    save_state
    log "queues appear stalled: wait=${wait_total} active=${active_total} streak=${streak}/${REQUIRED_STREAK}"
    if (( streak >= REQUIRED_STREAK )); then
        restart_jobs "queues stalled for ${streak} consecutive checks (wait=${wait_total})"
    fi
    exit 0
fi

reset_streak
exit 0
