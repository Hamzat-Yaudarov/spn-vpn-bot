#!/bin/bash
set -euo pipefail
. /root/vk-rehome-de177/common.sh

bootstrap_current
before=$(firewall_status)
active_firewall "$before"
backup=$stage/ufw.before-direct.txt
absent "$backup"
set -o noclobber
printf '%s\n' "$before" > "$backup"
set +o noclobber
for port in 80 443; do
    # Do not append behind an explicit port-specific policy that isn't a public allow.
    if grep -E "^$port(/tcp)?([[:space:]]|$)" <<< "$before" |
        grep -Ev "^$port/tcp([[:space:]]+\(v6\))?[[:space:]]+ALLOW IN[[:space:]]+Anywhere([[:space:]]+\(v6\))?$"; then
        fail "TCP $port has a different existing policy; review manually"
    fi
done
for port in 80 443; do
    if ! public_rule "$port" "$before"; then ufw allow "$port/tcp"; fi
done
after=$(firewall_status)
active_firewall "$after"
for port in 80 443; do public_rule "$port" "$after" || fail "missing TCP $port"; done
old_protected=$(grep -E '^(22|2222|2443)(/|[[:space:]])' <<< "$before" || true)
new_protected=$(grep -E '^(22|2222|2443)(/|[[:space:]])' <<< "$after" || true)
[[ "$old_protected" == "$new_protected" ]] || fail 'SSH, panel or VK firewall rules changed concurrently'
echo "Only TCP 80/443 direct ingress ensured; saved previous rules in $backup."
