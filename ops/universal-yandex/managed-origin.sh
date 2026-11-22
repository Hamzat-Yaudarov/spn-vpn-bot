#!/bin/bash
# Called only over authenticated SSH. Owns one dedicated Yandex CDN origin.
set -euo pipefail
umask 077
export LC_ALL=C
fail() { printf 'Stopped: %s\n' "$*" >&2; exit 1; }
valid_ipv4() {
    local value=$1 a b c d extra octet
    [[ "$value" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    IFS=. read -r a b c d extra <<< "$value"
    [ -z "${extra:-}" ] || return 1
    for octet in "$a" "$b" "$c" "$d"; do
        [[ "$octet" = 0 || "$octet" =~ ^[1-9][0-9]{0,2}$ ]] || return 1
        (( 10#$octet <= 255 )) || return 1
    done
}
valid_port() { [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && (( $1 <= 65535 )); }
[ "$#" = 6 ] || fail 'Expected action, origin domain, target IPv4, panel IPv4, SSH port and deployment ID'
ACTION=$1; DOMAIN=$2; NODE_IP=$3; PANEL_IP=$4; SSH_PORT=$5; DEPLOY_ID=$6
case "$ACTION" in preflight|packages|install|verify|identity) ;; *) fail 'Unknown origin action' ;; esac
[[ "$DEPLOY_ID" =~ ^[0-9a-f]{32}$ ]] || fail 'Invalid deployment ID'
[[ "$DOMAIN" =~ ^[a-z0-9][a-z0-9.-]+[a-z0-9]$ ]] || fail 'Invalid origin domain'
valid_ipv4 "$NODE_IP" && valid_ipv4 "$PANEL_IP" && [ "$NODE_IP" != "$PANEL_IP" ] || fail 'Invalid target or panel IPv4'
valid_port "$SSH_PORT" || fail 'Invalid SSH port'
case "$SSH_PORT" in 80|443|2222|10050|10085) fail 'SSH conflicts with an origin listener' ;; esac
IMAGE=remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e
STAGE=/root/spn-yandex-managed-$DEPLOY_ID
NODE=/opt/spn-yandex-cdn
SITE=/etc/nginx/sites-available/spn-yandex-cdn.conf
ENABLED=/etc/nginx/sites-enabled/spn-yandex-cdn.conf
ACME=/var/www/spn-yandex-acme
HOOK=/etc/letsencrypt/renewal-hooks/deploy/spn-yandex-nginx
PARAMS="$DOMAIN|$NODE_IP|$PANEL_IP|$SSH_PORT|$DEPLOY_ID"
command -v python3 >/dev/null || fail 'Ubuntu python3 is required'
python3 -c 'import ipaddress,re,sys
name=sys.argv[1]; addresses=[ipaddress.IPv4Address(x) for x in sys.argv[2:]]
ok=(len(name)<=253 and len(name.split("."))>=2 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",x) for x in name.split(".")) and all(x.is_global and not x.is_multicast for x in addresses))
raise SystemExit(0 if ok else 1)' "$DOMAIN" "$NODE_IP" "$PANEL_IP" || fail 'Invalid public origin identity'
[ "$(id -u)" = 0 ] || fail 'Root is required'
. /etc/os-release
[ "$ID:$VERSION_ID" = ubuntu:24.04 ] || fail 'Ubuntu 24.04 is required'

safe_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u:%a:%h' "$1")" = 0:600:1 ] || fail 'Unsafe managed file'
}
safe_dir() {
    [ -d "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u:%a' "$1")" = 0:700 ] || fail 'Unsafe managed directory'
}
safe_dir "$STAGE"
safe_file "$STAGE/host_checks.py"; safe_file "$STAGE/host-policy.json"
monitoring=$(python3 "$STAGE/host_checks.py" monitoring "$STAGE/host-policy.json") || fail 'Cannot verify or preserve monitoring'
case "$monitoring" in '[]') MONITOR_PORT='' ;; '[10050]') MONITOR_PORT=10050 ;; *) fail 'Invalid monitoring policy' ;; esac

owned_node() {
    [ -d "$NODE" ] && [ ! -L "$NODE" ] && [ "$(stat -c '%u:%a' "$NODE")" = 0:700 ] \
        && [ -f "$NODE/.spn-deployment" ] && [ ! -L "$NODE/.spn-deployment" ] \
        && [ "$(stat -c '%u:%a:%h' "$NODE/.spn-deployment")" = 0:600:1 ] \
        && [ "$(cat "$NODE/.spn-deployment")" = "$DEPLOY_ID" ]
}
owned_parameters() {
    safe_file "$NODE/.spn-parameters"
    [ "$(cat "$NODE/.spn-parameters")" = "$PARAMS" ] || fail 'Owned origin parameters differ'
}
install_owned() {
    local source=$1 target=$2 mode=${3:-0600}
    safe_file "$source"
    if [ -e "$target" ] || [ -L "$target" ]; then
        [ -f "$target" ] && [ ! -L "$target" ] && [ "$(stat -c '%u:%h' "$target")" = 0:1 ] \
            && [ "$(stat -c '%a' "$target")" = "${mode#0}" ] \
            && cmp -s "$source" "$target" || fail 'Managed target differs; refusing overwrite'
    else install -m "$mode" "$source" "$target"; fi
}
target_network() {
    ip -o -4 addr show up scope global | awk -v ip="$NODE_IP" '
        {split($4,a,"/"); if(a[1]==ip) n++} END {exit n!=1}' || fail 'Target IPv4 must be assigned once to an UP interface'
}
listeners_safe() {
    local owned=$1
    ss -H -lnut | awk -v owned="$owned" -v ssh="$SSH_PORT" -v monitor="$MONITOR_PORT" '
        {
            address=$5; port=address; sub(/^.*:/,"",port); sub(/:[^:]*$/,"",address); sub(/%.*/,"",address)
            if ($1=="tcp" && (port==ssh || (monitor!="" && port==monitor))) next
            if (address ~ /^127\./ || address=="[::1]" || address=="::1" || address ~ /^\[?::ffff:127\./) next
            if ($1=="udp" && (port=="68" || port=="546")) next
            if (owned=="1" && $1=="tcp" && (port=="80" || port=="443" || port=="2222")) next
            bad=1
        } END {exit bad}' || fail 'An unrelated public listener exists'
}
check_wireguard() {
    local names
    names=$(python3 "$STAGE/host_checks.py" wireguard) || fail 'Cannot inspect host interfaces'
    [ "$names" = '[]' ] || fail 'An unrelated WireGuard interface exists'
    if [ -e /etc/wireguard ] || [ -L /etc/wireguard ]; then
        [ -d /etc/wireguard ] && [ ! -L /etc/wireguard ] || fail 'Unsafe WireGuard directory'
        [ -z "$(find /etc/wireguard -mindepth 1 -maxdepth 1 -print -quit)" ] || fail 'Unrelated WireGuard files exist'
    fi
}
check_containers() {
    local row
    if ! command -v docker >/dev/null; then return; fi
    if ! docker info >/dev/null 2>&1; then
        owned_node || fail 'An unowned Docker installation exists'
        [ ! -f "$NODE/install-ok" ] || fail 'Owned Docker daemon is unavailable'
        return
    fi
    owned_node || fail 'An unowned Docker daemon exists'
    for row in $(docker ps -aq); do
        [ "$(docker inspect -f '{{.Name}}|{{index .Config.Labels "spn.deploy.id"}}|{{index .Config.Labels "spn.deploy.mode"}}|{{.Config.Image}}|{{.HostConfig.NetworkMode}}' "$row")" = \
            "/remnanode|$DEPLOY_ID|yandex-cdn|$IMAGE|host" ] || fail 'An unrelated container exists'
    done
}
check_sites() {
    local file
    shopt -s nullglob
    for file in /etc/nginx/sites-enabled/*; do
        if [ "$file" = "$ENABLED" ]; then
            owned_node && [ -L "$file" ] && [ "$(readlink "$file")" = "$SITE" ] || fail 'Unowned Nginx site link'
        elif [ "$file" = "/etc/nginx/sites-enabled/spn-yandex-extra-$DEPLOY_ID.conf" ]; then
            owned_node && [ -L "$file" ] || fail 'Unowned extra origin site link'
            safe_file "$STAGE/origin_bindings.py"
            safe_file "$STAGE/nginx.conf"
            python3 "$STAGE/origin_bindings.py" verify --ip "$NODE_IP" --domain "$DOMAIN" \
                --deployment "$DEPLOY_ID" --base-sha256 "$(sha256sum "$STAGE/nginx.conf" | awk '{print $1}')" \
                >/dev/null || fail 'Extra origin listener ownership or health is not confirmed'
        else
            [ "$file" = /etc/nginx/sites-enabled/default ] && [ -L "$file" ] \
                && [ "$(readlink "$file")" = /etc/nginx/sites-available/default ] \
                && [ ! -f "$NODE/install-ok" ] || fail 'Custom Nginx site exists'
        fi
    done
    for file in /etc/nginx/conf.d/*; do fail 'Custom Nginx include exists'; done
    if ! owned_node && [ -f /etc/nginx/nginx.conf ]; then
        [ -z "$(dpkg --verify nginx-common)" ] || fail 'Unowned packaged Nginx configuration was modified'
    fi
}
fresh_firewall_safe() {
    local tool
    if command -v ufw >/dev/null; then
        ! ufw status | grep -Fxq 'Status: active' || fail 'An unowned active firewall exists'
        ! ufw show added | grep -Eq '^ufw[[:space:]]' || fail 'Unowned stored firewall rules exist'
        [ -z "$(dpkg --verify ufw)" ] || fail 'Installed UFW package configuration changed'
    fi
    if command -v nft >/dev/null; then [ -z "$(nft list ruleset)" ] || fail 'Unowned nftables rules exist'; fi
    for tool in iptables-save ip6tables-save; do
        if command -v "$tool" >/dev/null; then
            "$tool" | awk '/^-/ {bad=1} /^:/ && $2!="ACCEPT" {bad=1} END {exit bad}' || fail 'Unowned netfilter rules exist'
        fi
    done
}
firewall_rules_safe() {
    local mode=${1:-complete}
    python3 -c 'import shlex,sys
ssh,panel,monitor,mode=sys.argv[1:]
expected={("tcp",ssh,"any","any"),("tcp","80","any","any"),("tcp","443","any","any"),("tcp","2222",panel,"any")}
if monitor: expected.add(("tcp",monitor,"any","any"))
seen=[]
try:
 for line in sys.stdin:
  if not line.startswith("ufw "): continue
  t=shlex.split(line)[1:]
  if "comment" in t: t=t[:t.index("comment")]
  if not t or t.pop(0)!="allow": raise ValueError()
  if t and t[0]=="in": t.pop(0)
  v={"proto":None,"port":None,"from":"any","to":"any"}; assigned=set()
  while t:
   token=t.pop(0)
   if token in v:
    if not t or token in assigned: raise ValueError()
    assigned.add(token); v[token]=t.pop(0)
   elif "/" in token and v["port"] is None:
    v["port"],v["proto"]=token.split("/"); assigned.update(("port","proto"))
   else: raise ValueError()
  for k in ("from","to"):
   if v[k].endswith("/32"): v[k]=v[k][:-3]
   if v[k]=="0.0.0.0/0": v[k]="any"
  seen.append(tuple(v[k] for k in ("proto","port","from","to")))
 if len(seen)!=len(set(seen)) or not set(seen)<=expected: raise ValueError()
 if mode not in ("complete","pending") or (mode=="complete" and set(seen)!=expected): raise ValueError()
except (ValueError,IndexError): raise SystemExit(1)' "$SSH_PORT" "$PANEL_IP" "$MONITOR_PORT" "$mode"
}
firewall_verified() {
    ufw status | grep -Fxq 'Status: active' || fail 'Owned firewall is inactive'
    ufw status verbose | grep -Fq 'Default: deny (incoming), allow (outgoing),' || fail 'Owned firewall defaults changed'
    ufw show added | firewall_rules_safe || fail 'Owned firewall rules differ'
    safe_file "$NODE/firewall.sha256"
    sha256sum --check --status "$NODE/firewall.sha256" || fail 'Owned firewall source files changed'
}
apt_install() { apt-get -o DPkg::Lock::Timeout=600 install -y "$@"; }
apt_update() {
    local attempt status output
    for ((attempt=0; attempt<=120; attempt++)); do
        if output=$(apt-get -o DPkg::Lock::Timeout=600 update 2>&1); then printf '%s\n' "$output"; return 0; else status=$?; fi
        if [[ "$output" != *"Could not get lock"* && "$output" != *"Unable to acquire"* ]]; then printf '%s\n' "$output" >&2; return "$status"; fi
        if [ "$attempt" = 120 ]; then printf '%s\n' "$output" >&2; return "$status"; fi
        if [ "$attempt" = 0 ]; then printf 'Waiting for Ubuntu package locks (up to ten minutes)…\n'; fi
        sleep 5
    done
}
write_docker_config() {
    local temp
    temp=$(mktemp "$STAGE/.docker.XXXXXXXX")
    printf '%s\n' '{"bridge":"none","iptables":false,"ip6tables":false,"ip-forward":false,"ip-masq":false}' > "$temp"
    install_owned "$temp" "$NODE/docker-daemon.json"
    if [ ! -e /etc/docker ]; then install -d -m 0755 /etc/docker; fi
    [ -d /etc/docker ] && [ ! -L /etc/docker ] && [ "$(stat -c '%u' /etc/docker)" = 0 ] || fail 'Unsafe Docker configuration directory'
    install_owned "$temp" /etc/docker/daemon.json
    rm "$temp"
}
install_packages() {
    local service masks
    if [ -f "$NODE/packages-ok" ]; then
        safe_file "$NODE/packages-ok"
        command -v docker >/dev/null && docker compose version >/dev/null && command -v nginx >/dev/null \
            && command -v certbot >/dev/null && command -v ufw >/dev/null || fail 'Owned package is missing'
        return
    fi
    masks=$(printf '%s\n' docker.service docker.socket containerd.service nginx.service)
    if [ -e "$NODE/package-masks" ] || [ -L "$NODE/package-masks" ]; then
        safe_file "$NODE/package-masks"; [ "$(cat "$NODE/package-masks")" = "$masks" ] || fail 'Owned package masks differ'
        for service in docker.service docker.socket containerd.service nginx.service; do
            if [ -e "/run/systemd/system/$service" ] || [ -L "/run/systemd/system/$service" ]; then
                [ -L "/run/systemd/system/$service" ] && [ "$(readlink "/run/systemd/system/$service")" = /dev/null ] || fail 'Foreign service override'
            fi
        done
    else
        for service in docker.service docker.socket containerd.service nginx.service; do
            [ ! -e "/run/systemd/system/$service" ] && [ ! -L "/run/systemd/system/$service" ] || fail 'Foreign service override'
        done
        printf '%s\n' "$masks" > "$NODE/package-masks"
    fi
    write_docker_config
    for service in docker.service docker.socket containerd.service nginx.service; do systemctl mask --runtime "$service"; done
    export DEBIAN_FRONTEND=noninteractive
    apt_update
    apt_install ca-certificates python3 docker.io docker-compose-v2 nginx certbot ufw
    for service in docker.service docker.socket containerd.service nginx.service; do systemctl unmask --runtime "$service"; done
    docker compose version >/dev/null
    touch "$NODE/packages-ok"
    rm "$NODE/package-masks"
}
claim_origin() {
    if [ -e "$NODE" ] || [ -L "$NODE" ]; then owned_node || fail 'Existing origin ownership differs'; owned_parameters; listeners_safe 1
    else
        listeners_safe 0; fresh_firewall_safe; check_containers; check_sites; check_wireguard
        local claim
        claim=$(mktemp -d /opt/.spn-yandex-claim.XXXXXXXX)
        printf '%s\n' "$DEPLOY_ID" > "$claim/.spn-deployment"
        printf '%s\n' "$PARAMS" > "$claim/.spn-parameters"
        mv -Tn "$claim" "$NODE"
        if [ -d "$claim" ]; then rm "$claim/.spn-deployment" "$claim/.spn-parameters"; rmdir "$claim"; fi
    fi
    owned_node || fail 'Could not claim owned origin'
}
validate_compose() {
    docker compose -f "$NODE/docker-compose.yml" config --format json | python3 -c 'import json,sys
try:
 d=json.load(sys.stdin); services=d["services"]
 if set(services)!={"remnanode"}: raise ValueError()
 s=services["remnanode"]
 if (s.get("image"),s.get("network_mode"),s.get("container_name"))!=(sys.argv[1],"host","remnanode"): raise ValueError()
 if s.get("labels",{}).get("spn.deploy.id")!=sys.argv[2] or s.get("labels",{}).get("spn.deploy.mode")!="yandex-cdn": raise ValueError()
 if any(s.get(k) for k in ("volumes","ports","privileged","cap_add","devices","entrypoint","command")): raise ValueError()
 if set(s.get("environment",{}))!={"NODE_PORT","SECRET_KEY"} or str(s["environment"]["NODE_PORT"])!="2222": raise ValueError()
 if s.get("ulimits",{}).get("nofile")!={"soft":65536,"hard":65536}: raise ValueError()
except (KeyError,ValueError,TypeError): raise SystemExit(1)' "$IMAGE" "$DEPLOY_ID" || fail 'Unexpected owned node configuration'
}
install_firewall() {
    if [ -f "$NODE/firewall.sha256" ]; then firewall_verified; return; fi
    ufw show added | firewall_rules_safe pending || fail 'Unowned firewall rules appeared'
    ufw allow "$SSH_PORT/tcp" comment SPN-YANDEX-SSH
    ufw allow 80/tcp comment SPN-YANDEX-ACME
    ufw allow 443/tcp comment SPN-YANDEX-TLS
    ufw allow from "$PANEL_IP" to any port 2222 proto tcp comment SPN-YANDEX-PANEL
    if [ -n "$MONITOR_PORT" ]; then ufw allow "$MONITOR_PORT/tcp" comment SPN-YANDEX-EXISTING-MONITORING; fi
    ufw show added | firewall_rules_safe || fail 'Unexpected firewall rules appeared'
    ufw default deny incoming; ufw default allow outgoing; ufw --force enable
    sha256sum /etc/ufw/{ufw.conf,before.rules,after.rules,user.rules,before6.rules,after6.rules,user6.rules} /etc/default/ufw > "$NODE/firewall.sha256"
    firewall_verified
}
claim_nginx_main() {
    safe_file "$STAGE/nginx-main.conf"
    if [ ! -e "$NODE/nginx-main.before" ]; then
        [ -f /etc/nginx/nginx.conf ] && [ ! -L /etc/nginx/nginx.conf ] && [ "$(stat -c '%u' /etc/nginx/nginx.conf)" = 0 ] || fail 'Unsafe packaged Nginx configuration'
        [ -z "$(dpkg --verify nginx-common)" ] || fail 'Packaged Nginx configuration was modified before ownership'
        install -m 0600 /etc/nginx/nginx.conf "$NODE/nginx-main.before"
    fi
    safe_file "$NODE/nginx-main.before"
    if [ -e "$NODE/nginx-main.expected" ]; then
        safe_file "$NODE/nginx-main.expected"
        cmp -s "$NODE/nginx-main.expected" "$STAGE/nginx-main.conf" || fail 'Staged Nginx main configuration changed'
    else install -m 0600 "$STAGE/nginx-main.conf" "$NODE/nginx-main.expected"; fi
    cmp -s /etc/nginx/nginx.conf "$NODE/nginx-main.before" || cmp -s /etc/nginx/nginx.conf "$NODE/nginx-main.expected" || fail 'Nginx main configuration changed'
    nginx -t -c "$STAGE/nginx-main.conf" || fail 'Candidate Nginx main configuration is invalid'
    install -m 0644 "$STAGE/nginx-main.conf" /etc/nginx/nginx.conf
}
install_site() {
    if [ -e "$SITE" ] || [ -L "$SITE" ]; then
        [ -f "$SITE" ] && [ ! -L "$SITE" ] && { cmp -s "$SITE" "$STAGE/bootstrap.conf" || cmp -s "$SITE" "$STAGE/nginx.conf"; } || fail 'Nginx origin site changed'
    else install -m 0644 "$STAGE/bootstrap.conf" "$SITE"; fi
    if [ -L /etc/nginx/sites-enabled/default ]; then
        [ "$(readlink /etc/nginx/sites-enabled/default)" = /etc/nginx/sites-available/default ] || fail 'Unexpected default site'
        if [ ! -f "$NODE/default-site.before" ]; then printf '%s\n' /etc/nginx/sites-available/default > "$NODE/default-site.before"; fi
        rm /etc/nginx/sites-enabled/default
    fi
    if [ ! -L "$ENABLED" ]; then [ ! -e "$ENABLED" ] || fail 'Unexpected origin site entry'; ln -s "$SITE" "$ENABLED"; fi
    install -d -m 0755 "$ACME"
    nginx -t
    systemctl enable --now nginx
    systemctl reload nginx
}
listener_exact() {
    ss -H -lnt "sport = :$1" | awk -v expected="$2:$1" 'NF {n++; if($4!=expected) bad=1} END {exit !(n==1 && !bad)}'
}
probe_tcp() {
    python3 -c 'import socket,sys
try:
 s=socket.create_connection((sys.argv[1],int(sys.argv[2])),1); s.close()
except OSError: raise SystemExit(1)' "$1" "$2"
}
verify_owned_files() {
    local file
    for file in "$NODE/node.env" "$NODE/docker-compose.yml" "$NODE/docker-daemon.json" /etc/docker/daemon.json \
                "$SITE" /etc/nginx/nginx.conf "$HOOK"; do
        [ -f "$file" ] && [ ! -L "$file" ] && [ "$(stat -c '%u:%h' "$file")" = 0:1 ] || fail 'Unsafe owned origin file'
        python3 -c 'import os,sys; raise SystemExit(1 if os.stat(sys.argv[1]).st_mode & 0o022 else 0)' "$file" || fail 'Owned origin file is writable by others'
    done
    safe_file "$NODE/files.sha256"
    sha256sum --check --status "$NODE/files.sha256" || fail 'Owned origin files changed'
    cmp -s "$NODE/docker-daemon.json" /etc/docker/daemon.json || fail 'Owned Docker settings changed'
    check_sites
}
record_owned_files() {
    if [ -f "$NODE/files.sha256" ]; then verify_owned_files; return; fi
    sha256sum "$NODE/node.env" "$NODE/docker-compose.yml" "$NODE/docker-daemon.json" /etc/docker/daemon.json \
        "$SITE" /etc/nginx/nginx.conf "$HOOK" > "$NODE/files.sha256"
}

case "$ACTION" in
identity)
    [ -f /etc/machine-id ] && [ ! -L /etc/machine-id ] || fail 'Machine identity missing'
    [ -f /etc/ssh/ssh_host_ed25519_key.pub ] || fail 'ED25519 host key missing'
    machine=$(tr -d '\n' < /etc/machine-id)
    fingerprint=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256 | awk '{print $2}')
    [[ "$machine" =~ ^[0-9a-f]{32}$ && "$fingerprint" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || fail 'Invalid machine identity'
    if owned_node; then managed=true; else managed=false; fi
    printf '{"machineId":"%s","sshFingerprint":"%s","ownedInstallation":%s}\n' "$machine" "$fingerprint" "$managed"
    ;;
preflight)
    target_network; check_sites; check_containers; check_wireguard
    if [ -e "$NODE" ] || [ -L "$NODE" ]; then
        owned_node || fail 'Existing CDN origin is not owned by this deployment'
        owned_parameters; listeners_safe 1
        if [ -f "$NODE/firewall.sha256" ]; then firewall_verified
        elif command -v ufw >/dev/null; then ufw show added | firewall_rules_safe pending || fail 'Unknown pending firewall state'; fi
        if [ -f "$NODE/files.sha256" ]; then verify_owned_files; fi
    else
        listeners_safe 0; fresh_firewall_safe
        for p in nginx.service docker.service containerd.service; do
            if systemctl is-active --quiet "$p"; then fail 'An unrelated package service is already active'; fi
        done
        for p in /opt/remnanode /opt/spn-cascade /opt/spn-cascade-ru /opt/spn-reality-node /opt/spn-direct-node \
                 /var/lib/docker /var/lib/containerd /etc/docker/daemon.json /etc/letsencrypt; do
            [ ! -e "$p" ] && [ ! -L "$p" ] || fail 'An older node, certificate or container installation exists'
        done
        [ ! -e "$SITE" ] && [ ! -L "$SITE" ] || fail 'Unowned CDN origin site exists'
    fi
    if [ -e "$STAGE/preflight-ok" ]; then safe_file "$STAGE/preflight-ok"; [ "$(cat "$STAGE/preflight-ok")" = "$PARAMS" ] || fail 'Saved parameters differ'
    else printf '%s\n' "$PARAMS" > "$STAGE/preflight-ok"; fi
    printf 'Yandex origin preflight OK\n'
    ;;
packages)
    target_network; safe_file "$STAGE/preflight-ok"
    [ "$(cat "$STAGE/preflight-ok")" = "$PARAMS" ] || fail 'Preflight parameters differ'
    claim_origin
    install_packages
    printf 'Yandex origin packages ready\n'
    ;;
install)
    target_network; safe_file "$STAGE/preflight-ok"
    [ "$(cat "$STAGE/preflight-ok")" = "$PARAMS" ] || fail 'Preflight parameters differ'
    for p in node.env docker-compose.yml bootstrap.conf nginx.conf nginx-main.conf renew-nginx.sh; do safe_file "$STAGE/$p"; done
    claim_origin
    check_sites; check_containers; check_wireguard
    install_owned "$STAGE/node.env" "$NODE/node.env"
    install_owned "$STAGE/docker-compose.yml" "$NODE/docker-compose.yml"
    install_packages
    validate_compose
    install_firewall
    claim_nginx_main
    install_site
    if [ -e "$STAGE/tls-certificate.bundle.json" ] || [ -L "$STAGE/tls-certificate.bundle.json" ]; then
        safe_file "$STAGE/tls-certificate.bundle.json"; safe_file "$STAGE/tls_certificate_import.py"
        python3 "$STAGE/tls_certificate_import.py" --bundle "$STAGE/tls-certificate.bundle.json" \
            --deployment "$DEPLOY_ID" --domain "$DOMAIN" --mode yandex-cdn
    fi
    cert=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
    if [ ! -s "$cert" ] || ! openssl x509 -in "$cert" -noout -checkend 86400 >/dev/null; then
        certbot certonly --webroot -w "$ACME" -d "$DOMAIN" --cert-name "$DOMAIN" \
            --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring
    fi
    openssl x509 -in "$cert" -noout -checkhost "$DOMAIN" >/dev/null || fail 'Origin certificate name differs'
    install -m 0644 "$STAGE/nginx.conf" "$SITE"
    if ! nginx -t; then install -m 0644 "$STAGE/bootstrap.conf" "$SITE"; fail 'TLS origin configuration failed; bootstrap restored'; fi
    install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
    install_owned "$STAGE/renew-nginx.sh" "$HOOK" 0755
    systemctl enable --now certbot.timer
    systemctl reload nginx
    systemctl enable --now containerd docker
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then docker pull "$IMAGE"; fi
    docker compose -f "$NODE/docker-compose.yml" up -d --pull never
    record_owned_files
    touch "$NODE/install-ok"
    printf 'Yandex HTTPS origin installed; CDN is still a manual step\n'
    ;;
verify)
    target_network; owned_node || fail 'Missing owned origin'; owned_parameters
    verify_owned_files; check_containers; check_wireguard; firewall_verified; listeners_safe 1
    validate_compose; nginx -t; systemctl is-active --quiet nginx
    [ "$(docker inspect -f '{{.State.Running}}' remnanode)" = true ] || fail 'Owned node is stopped'
    openssl x509 -in /etc/letsencrypt/live/$DOMAIN/fullchain.pem -noout -checkhost "$DOMAIN" >/dev/null
    openssl x509 -in /etc/letsencrypt/live/$DOMAIN/fullchain.pem -noout -checkend 86400 >/dev/null || fail 'Origin certificate is expiring'
    ready=0
    for ((attempt=0; attempt<90; attempt++)); do
        if listener_exact 10085 127.0.0.1 && probe_tcp "$NODE_IP" 2222 && probe_tcp "$NODE_IP" 443; then ready=1; break; fi
        sleep 1
    done
    [ "$ready" = 1 ] || fail 'Origin TLS, loopback XHTTP or node API is not ready after 90 seconds; repeat the same command'
    printf 'Origin TLS, exact firewall and loopback XHTTP verified; CDN/client tests are still required\n'
    ;;
esac
