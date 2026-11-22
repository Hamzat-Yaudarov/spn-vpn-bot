#!/bin/bash
set -euo pipefail
. /root/direct-node18253/common.sh

# Only the distribution default Nginx site may be moved; it is backed up.
backup=$stage/nginx-default.enabled.before
for name in bootstrap.conf nginx.conf docker-compose.yml renew-nginx.sh node.env; do safe_file "$stage/$name"; done
[ "$(stat -c %a "$stage/node.env")" = 600 ] || fail 'node.env must be 0600'
for path in "$site" "$enabled" "$backup" /opt/remnanode "$webroot"; do absent "$path"; done
for path in /etc/nginx/sites-enabled/*; do
    [ -e "$path" ] || [ -L "$path" ] || continue
    [ "$path" = /etc/nginx/sites-enabled/default ] && [ -L "$path" ] \
        && [ "$(readlink "$path")" = /etc/nginx/sites-available/default ] || fail 'unexpected enabled Nginx site'
done
free_ports 443 2222 10087 10088
docker compose version >/dev/null
docker info >/dev/null
if docker inspect remnanode >/dev/null 2>&1; then fail 'container name already exists'; fi
nginx -t
systemctl is-active --quiet nginx
default_moved=0
rollback() {
    local code=$?
    if [ "$code" -ne 0 ]; then
        rm -f -- "$enabled" "$site" /opt/remnanode/node.env "$compose"
        rmdir -- /opt/remnanode "$webroot" 2>/dev/null || true
        if [ "$default_moved" = 1 ]; then mv -- "$backup" /etc/nginx/sites-enabled/default; fi
        if nginx -t; then systemctl reload nginx || true; fi
        echo 'Bootstrap failed; new files rolled back. Inspect before retry.' >&2
    fi
    exit "$code"
}
trap rollback EXIT
install -d -m 0700 /opt/remnanode
install -d -m 0755 "$webroot"
install -m 0600 "$stage/node.env" /opt/remnanode/node.env
install -m 0600 "$stage/docker-compose.yml" "$compose"
install -m 0644 "$stage/bootstrap.conf" "$site"
if [ -L /etc/nginx/sites-enabled/default ]; then
    mv -- /etc/nginx/sites-enabled/default "$backup"
    default_moved=1
fi
ln -s -- "$site" "$enabled"
nginx -t
systemctl reload nginx
trap - EXIT
echo 'HTTP bootstrap installed. Firewall, TLS issuance and node activation are separate.'
