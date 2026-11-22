#!/bin/bash
# Certificate issuance/LE TOS acceptance belong to a separate owner command.
set -euo pipefail
umask 077
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
stage=/root/direct-deeee1
site=/etc/nginx/sites-available/deeee1.wayspn.com
backup=$stage/nginx-bootstrap.before-https.conf
hook=/etc/letsencrypt/renewal-hooks/deploy/direct-deeee1-nginx
cert=/etc/letsencrypt/live/deeee1.wayspn.com
fail() { echo "Stopped: $1" >&2; exit 1; }
start_node=0
case "$*" in
    --apply) ;;
    '--apply --start-node --confirm-panel-only 144.31.170.147') start_node=1 ;;
    *) fail 'use --apply; optional --start-node --confirm-panel-only 144.31.170.147 after firewall verification' ;;
esac
[ "$(id -u)" = 0 ] || fail 'root required'
ip -o -4 addr show scope global | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq 64.188.83.197 || fail 'wrong VPS address'
[ -d "$stage" ] && [ ! -L "$stage" ] && [ "$(stat -c '%u:%a' "$stage")" = 0:700 ] || fail 'unsafe staging directory'
for name in bootstrap.conf nginx.conf renew-nginx.sh; do
    file=$stage/$name
    [ -f "$file" ] && [ ! -L "$file" ] && [ "$(stat -c '%u:%h' "$file")" = 0:1 ] || fail 'unsafe staging file'
    mode=$(stat -c %a "$file")
    (( (8#$mode & 022) == 0 )) || fail 'writable staging file'
done
[ -f "$site" ] && [ ! -L "$site" ] && cmp -s "$stage/bootstrap.conf" "$site" || fail 'site is not the expected bootstrap'
[ -L /etc/nginx/sites-enabled/deeee1.wayspn.com ] && [ "$(readlink /etc/nginx/sites-enabled/deeee1.wayspn.com)" = "$site" ] || fail 'site link changed'
for path in "$backup" "$hook"; do
    [ ! -e "$path" ] && [ ! -L "$path" ] || fail 'HTTPS target already exists; inspect before retry'
done
for name in cert.pem chain.pem fullchain.pem privkey.pem; do [ -s "$cert/$name" ] || fail 'certificate not installed'; done
openssl x509 -in "$cert/cert.pem" -noout -checkend 86400 >/dev/null || fail 'certificate expires too soon'
san=$(openssl x509 -in "$cert/cert.pem" -noout -ext subjectAltName | tail -n +2 | tr -d '[:space:]')
[ "$san" = DNS:deeee1.wayspn.com ] || fail 'certificate SAN differs'
# Validate the same leaf and intermediates that Nginx will actually serve.
[ "$(openssl x509 -in "$cert/cert.pem" -noout -fingerprint -sha256)" = \
  "$(openssl x509 -in "$cert/fullchain.pem" -noout -fingerprint -sha256)" ] || fail 'served certificate leaf differs'
openssl verify -CApath /etc/ssl/certs -untrusted "$cert/fullchain.pem" "$cert/cert.pem" >/dev/null || fail 'certificate chain not trusted'
if [ "$start_node" = 1 ]; then
    [ "$(stat -c '%u:%a:%h' /opt/remnanode/node.env)" = 0:600:1 ] && [ ! -L /opt/remnanode/node.env ] || fail 'unsafe installed node.env'
    cmp -s "$stage/docker-compose.yml" /opt/remnanode/docker-compose.yml || fail 'compose changed'
    if docker inspect remnanode >/dev/null 2>&1; then fail 'container already exists'; fi
    docker image inspect remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e >/dev/null || fail 'pinned image must be pulled separately'
fi
nginx -t
cp -p -- "$site" "$backup"
chmod 0600 "$backup"
rollback() {
    code=$?
    if [ "$code" -ne 0 ]; then
        cp -- "$backup" "$site"
        chmod 0644 "$site"
        rm -f -- "$hook"
        if nginx -t; then systemctl reload nginx || true; fi
        echo 'HTTPS activation failed; bootstrap restored. Inspect before retry.' >&2
    fi
    exit "$code"
}
trap rollback EXIT
install -m 0644 "$stage/nginx.conf" "$site"
# nginx -t also validates that the private key matches the certificate.
nginx -t
install -m 0755 "$stage/renew-nginx.sh" "$hook"
systemctl reload nginx
trap - EXIT
if [ "$start_node" = 1 ]; then
    # Host network: TCP2222 must already be reachable ONLY from panel144.31.170.147.
    # Root separately verifies TCP22/80/443/2083; no UDP8443/HY2 is used.
    docker compose -f /opt/remnanode/docker-compose.yml up -d --pull never --no-build
fi
echo 'HTTPS installed. Health is keyless only; verify WS, REALITY and real client connectivity separately.'
