#!/bin/bash
set -euo pipefail
. /root/direct-ch121/common.sh

backup=$stage/nginx-bootstrap.before-https.conf
hook=/etc/letsencrypt/renewal-hooks/deploy/direct-ch121-nginx
cert=/etc/letsencrypt/live/$domain
for name in bootstrap.conf nginx.conf renew-nginx.sh docker-compose.yml node.env; do safe_file "$stage/$name"; done
safe_file "$site"
cmp -s "$stage/bootstrap.conf" "$site" || fail 'site is not the expected bootstrap'
[ -L "$enabled" ] && [ "$(readlink "$enabled")" = "$site" ] || fail 'site link changed'
absent "$backup"
absent "$hook"
for name in cert.pem chain.pem fullchain.pem privkey.pem; do [ -s "$cert/$name" ] || fail 'certificate not installed'; done
openssl x509 -in "$cert/cert.pem" -noout -checkend 86400 >/dev/null || fail 'certificate expires too soon'
[ "$(openssl x509 -in "$cert/cert.pem" -noout -ext subjectAltName | tail -n +2 | tr -d '[:space:]')" = "DNS:$domain" ] || fail 'unexpected certificate SAN'
[ "$(openssl x509 -in "$cert/cert.pem" -noout -fingerprint -sha256)" = "$(openssl x509 -in "$cert/fullchain.pem" -noout -fingerprint -sha256)" ] || fail 'unexpected served leaf'
openssl verify -CApath /etc/ssl/certs -untrusted "$cert/fullchain.pem" "$cert/cert.pem" >/dev/null || fail 'untrusted certificate'
verify_panel_ip
# UFW pads table rows on some Ubuntu builds; validate the same rule set.
status=$(ufw status verbose | sed 's/[[:blank:]]*$//')
grep -Fxq 'Status: active' <<< "$status" || fail 'firewall must be active'
grep -Eq '^Default: deny \(incoming\),' <<< "$status" || fail 'expected default deny incoming'
for port in 22 80 443; do
    grep -Eq "^$port/tcp[[:space:]]+ALLOW IN[[:space:]]+Anywhere$" <<< "$status" || fail "TCP $port not allowed"
done
grep -Eq '^2222/tcp[[:space:]]+ALLOW IN[[:space:]]+144\.31\.170\.147$' <<< "$status" || fail 'panel control rule missing'
if grep -E '^([0-9]|Anywhere)' <<< "$status" |
    grep -Ev '^(22|80|443)/tcp([[:space:]]+\(v6\))?[[:space:]]+ALLOW IN[[:space:]]+Anywhere([[:space:]]+\(v6\))?$|^2222/tcp[[:space:]]+ALLOW IN[[:space:]]+144\.31\.170\.147$'; then
    fail 'unexpected firewall rules; review manually'
fi
free_ports 443
safe_file "$compose"
safe_file /opt/remnanode/node.env
[ "$(stat -c '%u:%a' /opt/remnanode)" = 0:700 ] || fail 'unsafe node directory'
[ "$(stat -c %a /opt/remnanode/node.env)" = 600 ] || fail 'unsafe installed node.env'
cmp -s "$stage/docker-compose.yml" "$compose" || fail 'compose changed'
cmp -s "$stage/node.env" /opt/remnanode/node.env || fail 'node.env changed'
docker info >/dev/null
docker image inspect "$image" >/dev/null || fail 'pinned image must be pulled separately'
already_running=false
if docker inspect remnanode >/dev/null 2>&1; then
    actual=$(docker inspect -f '{{.Config.Image}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{.HostConfig.NetworkMode}}|{{.State.Running}}' remnanode)
    [ "$actual" = "$image|/opt/remnanode|remnanode|host|true" ] || fail 'unexpected existing container'
    already_running=true
else
    free_ports 2222 10087 10088
fi
nginx -t
install -m 0600 "$site" "$backup"
rollback() {
    local code=$?
    if [ "$code" -ne 0 ]; then
        install -m 0644 "$backup" "$site"
        rm -f -- "$hook"
        if nginx -t; then systemctl reload nginx || true; fi
        echo 'TLS activation failed; bootstrap restored. Inspect before retry.' >&2
    fi
    exit "$code"
}
trap rollback EXIT
install -m 0644 "$stage/nginx.conf" "$site"
nginx -t
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
install -m 0755 "$stage/renew-nginx.sh" "$hook"
systemctl reload nginx
trap - EXIT
if ! $already_running; then
    docker compose -f "$compose" up -d --pull never --no-build
fi
echo 'TLS and node installed; verify panel status and both transport handshakes.'
