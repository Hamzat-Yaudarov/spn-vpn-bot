#!/bin/bash
set -euo pipefail
. /root/direct-node18253/common.sh

verify_panel_ip
[ -n "$(ss -H -lnt 'sport = :22')" ] || fail 'SSH is not listening on TCP 22'
status=$(ufw status)
grep -Fxq 'Status: inactive' <<< "$status" || fail 'existing active firewall; review manually'
added=$(ufw show added)
if grep -Eq '^ufw[[:space:]]' <<< "$added"; then fail 'existing UFW rules; review manually'; fi
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow from "$panel_ip" to any port 2222 proto tcp
ufw default deny incoming
ufw --force enable
ufw status verbose
