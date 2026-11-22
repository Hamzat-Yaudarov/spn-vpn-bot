#!/bin/bash
set -euo pipefail
umask 077
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
[ "$(id -u)" = 0 ]
. /etc/os-release
[ "$ID:$VERSION_ID" = ubuntu:24.04 ]
ip -o -4 addr show scope global | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq 177.3.250.75
[ ! -e /opt/remnanode ] && [ ! -e /opt/remnanode-single75 ]
for port in 443 2222; do [ -z "$(ss -H -lnt "sport = :$port")" ]; done
if ufw status | grep -Fxq 'Status: active'; then echo 'Existing firewall; inspect before installation' >&2; exit 2; fi
apt-get update
apt-get install -y ca-certificates curl docker.io docker-compose-v2 ufw
systemctl enable --now docker
docker compose version
printf 'Packages ready; no VPN listener yet\n'
