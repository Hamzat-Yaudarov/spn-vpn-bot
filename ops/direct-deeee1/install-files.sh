#!/bin/bash
# Fixed-target one-shot bootstrap. Does not issue certificates or start a node.
set -euo pipefail
umask 077
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
stage=/root/direct-deeee1
site=/etc/nginx/sites-available/deeee1.wayspn.com
enabled=/etc/nginx/sites-enabled/deeee1.wayspn.com
backup=$stage/nginx-default.enabled.before
fail() { echo "Stopped: $1" >&2; exit 1; }
[ "$#" -eq 1 ] && [ "$1" = --apply ] || fail 'explicit --apply required'
[ "$(id -u)" = 0 ] || fail 'root required'
ip -o -4 addr show scope global | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq 64.188.83.197 || fail 'wrong VPS address'
[ -d "$stage" ] && [ ! -L "$stage" ] && [ "$(stat -c '%u:%a' "$stage")" = 0:700 ] || fail 'staging must be root-owned 0700'
for name in bootstrap.conf nginx.conf docker-compose.yml renew-nginx.sh node.env; do
    file=$stage/$name
    [ -f "$file" ] && [ ! -L "$file" ] && [ "$(stat -c '%u:%h' "$file")" = 0:1 ] || fail 'unsafe staging file metadata'
    mode=$(stat -c %a "$file")
    (( (8#$mode & 022) == 0 )) || fail 'writable staging file'
done
[ "$(stat -c %a "$stage/node.env")" = 600 ] || fail 'node.env must be 0600'
for path in "$site" "$enabled" "$backup" /opt/remnanode /var/www/direct-deeee1-acme; do
    [ ! -e "$path" ] && [ ! -L "$path" ] || fail 'target already exists; inspect before retry'
done
# Only the package's expected default link may be displaced, reversibly.
for path in /etc/nginx/sites-enabled/*; do
    [ -e "$path" ] || [ -L "$path" ] || continue
    [ "$path" = /etc/nginx/sites-enabled/default ] && [ -L "$path" ] \
        && [ "$(readlink "$path")" = /etc/nginx/sites-available/default ] || fail 'unexpected enabled Nginx site'
done
docker compose version >/dev/null
if docker inspect remnanode >/dev/null 2>&1; then fail 'container name already exists'; fi
nginx -t
default_moved=0
created=0
rollback() {
    code=$?
    if [ "$code" -ne 0 ] && [ "$created" = 1 ]; then
        rm -f -- "$enabled" "$site"
        rm -f -- /opt/remnanode/node.env /opt/remnanode/docker-compose.yml
        rmdir -- /opt/remnanode /var/www/direct-deeee1-acme 2>/dev/null || true
        if [ "$default_moved" = 1 ]; then mv -- "$backup" /etc/nginx/sites-enabled/default; fi
        if nginx -t; then systemctl reload nginx || true; fi
        echo 'Bootstrap failed; newly installed paths rolled back. Inspect before retry.' >&2
    fi
    exit "$code"
}
trap rollback EXIT
created=1
install -d -m 0700 /opt/remnanode
install -d -m 0755 /var/www/direct-deeee1-acme
install -m 0600 "$stage/node.env" /opt/remnanode/node.env
install -m 0600 "$stage/docker-compose.yml" /opt/remnanode/docker-compose.yml
install -m 0644 "$stage/bootstrap.conf" "$site"
if [ -L /etc/nginx/sites-enabled/default ]; then
    mv -- /etc/nginx/sites-enabled/default "$backup"
    default_moved=1
fi
ln -s -- "$site" "$enabled"
nginx -t
systemctl reload nginx
trap - EXIT
echo 'HTTP ACME bootstrap installed. Certificate, firewall and node activation remain separate.'
