#!/bin/bash
set -euo pipefail
. /root/vk-rehome-de177/common.sh

bootstrap_current
for name in nginx.conf renew-nginx.sh; do safe_file "$stage/$name"; done
status=$(firewall_status)
active_firewall "$status"
for port in 80 443; do public_rule "$port" "$status" || fail "missing TCP $port"; done
free_port 443
cert=/etc/letsencrypt/live/$domain
for name in cert.pem chain.pem fullchain.pem privkey.pem; do
    [[ -s "$cert/$name" ]] || fail 'certificate not installed'
done
openssl x509 -in "$cert/cert.pem" -noout -checkend 86400 >/dev/null || fail 'certificate expires too soon'
[[ "$(openssl x509 -in "$cert/cert.pem" -noout -ext subjectAltName | tail -n +2 | tr -d '[:space:]')" == "DNS:$domain" ]] || fail 'unexpected certificate SAN'
[[ "$(openssl x509 -in "$cert/cert.pem" -noout -fingerprint -sha256)" == \
   "$(openssl x509 -in "$cert/fullchain.pem" -noout -fingerprint -sha256)" ]] || fail 'unexpected served leaf'
openssl verify -CApath /etc/ssl/certs -untrusted "$cert/chain.pem" "$cert/cert.pem" >/dev/null || fail 'untrusted certificate'
backup=$stage/nginx-bootstrap.before-https.conf
hook=/etc/letsencrypt/renewal-hooks/deploy/vk-rehome-de177-nginx
absent "$backup"
absent "$hook"
nginx -t
install -m 0600 "$site" "$backup"
rollback() {
    local code=$?
    trap - EXIT
    if (( code )); then
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
health=$(curl --fail --silent --show-error --max-time 5 --noproxy '*' --resolve "$domain:443:$address" "https://$domain/health")
[[ "$health" == '{"status":"ok"}' ]] || fail 'unexpected HTTPS health response'
trap - EXIT
echo 'Direct TLS front ready. Remnawave Node and VK ingress were not modified.'
