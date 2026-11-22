#!/bin/bash
# Shared guards for the existing German node; never deploys or restarts Xray.
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C
stage=/root/vk-rehome-de177
address=48.102.177.3
domain=de177.wayspn.com
webroot=/var/www/vk-rehome-de177-acme
site=/etc/nginx/sites-available/$domain
enabled=/etc/nginx/sites-enabled/$domain
default_site=/etc/nginx/sites-enabled/default
default_backup=$stage/nginx-default.enabled.before
fail() { printf 'Stopped: %s\n' "$1" >&2; exit 1; }
absent() { [[ ! -e "$1" && ! -L "$1" ]] || fail "already exists: $1"; }
safe_file() {
    [[ -f "$1" && ! -L "$1" && "$(stat -c '%u:%h' "$1")" == 0:1 ]] || fail "unsafe file: $1"
    local mode
    mode=$(stat -c '%a' "$1")
    (( (8#$mode & 022) == 0 )) || fail "writable file: $1"
}
free_port() {
    local listeners
    listeners=$(ss -H -lnt "sport = :$1")
    [[ -z "$listeners" ]] || fail "TCP $1 already in use"
}
firewall_status() { ufw status verbose | sed 's/[[:blank:]]*$//'; }
active_firewall() {
    grep -Fxq 'Status: active' <<< "$1" || fail 'UFW must already be active'
    grep -Eq '^Default: deny \(incoming\),' <<< "$1" || fail 'expected deny incoming'
}
public_rule() {
    grep -Eq "^$1/tcp[[:space:]]+ALLOW IN[[:space:]]+Anywhere$" <<< "$2"
}
bootstrap_current() {
    safe_file "$stage/bootstrap.conf"
    safe_file "$site"
    cmp -s "$stage/bootstrap.conf" "$site" || fail 'site is not the expected HTTP bootstrap'
    [[ -L "$enabled" && "$(readlink "$enabled")" == "$site" ]] || fail 'site link changed'
}

[[ $# -eq 1 && $1 == --apply ]] || fail 'explicit --apply required'
[[ $(id -u) == 0 ]] || fail 'root required'
[[ -d "$stage" && ! -L "$stage" && "$(stat -c '%u:%a' "$stage")" == 0:700 ]] || fail 'stage must be root-owned 0700'
ip -o -4 addr show scope global | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq "$address" || fail 'wrong VPS address'
[[ "$(docker inspect -f '{{.State.Running}}|{{.HostConfig.NetworkMode}}' remnanode)" == 'true|host' ]] || fail 'expected running host-network Remnawave Node'
