#!/bin/bash
# Source only from the fixed, root-owned staging directory.
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C
stage=/root/direct-ch121
ip_address=179.254.109.121
domain=ch121.wayspn.com
panel_ip=144.31.170.147
webroot=/var/www/direct-ch121-acme
site=/etc/nginx/sites-available/$domain
enabled=/etc/nginx/sites-enabled/$domain
compose=/opt/remnanode/docker-compose.yml
image=remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e
fail() { echo "Stopped: $1" >&2; exit 1; }
absent() { [ ! -e "$1" ] && [ ! -L "$1" ] || fail "already exists: $1"; }
safe_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u:%h' "$1")" = 0:1 ] || fail 'unsafe staging file metadata'
    local mode
    mode=$(stat -c %a "$1")
    (( (8#$mode & 022) == 0 )) || fail 'writable staging file'
}
verify_panel_ip() {
    [ "$(getent ahostsv4 panel.wayspn.online | awk '{print $1}' | sort -u)" = "$panel_ip" ] || fail 'panel IPv4 changed; review firewall rules'
}
free_ports() {
    local port listeners
    for port in "$@"; do
        listeners=$(ss -H -lnt "sport = :$port")
        [ -z "$listeners" ] || fail "TCP $port already in use"
    done
}
[ "$#" -eq 1 ] && [ "$1" = --apply ] || fail 'explicit --apply required'
[ "$(id -u)" = 0 ] || fail 'root required'
[ -d "$stage" ] && [ ! -L "$stage" ] && [ "$(stat -c '%u:%a' "$stage")" = 0:700 ] || fail 'staging must be root-owned 0700'
ip -o -4 addr show scope global | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq "$ip_address" || fail 'wrong VPS address'
