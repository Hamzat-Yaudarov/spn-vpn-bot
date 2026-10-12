#!/bin/bash
set -euo pipefail
stage=/root/direct-node29
domain=node29.prestizh.site
test -f "$stage/installed-bootstrap"
addresses=$(dig @1.1.1.1 +short A "$domain")
if [ "$addresses" != 64.188.78.29 ]; then
    echo 'DNS does not point exclusively to the new VPS; refusing certificate issuance.'
    exit 2
fi
if [ -n "$(dig @1.1.1.1 +short AAAA "$domain")" ]; then
    echo 'Unexpected AAAA record; inspect before issuance.'
    exit 2
fi
if [ ! -f "/etc/letsencrypt/live/$domain/fullchain.pem" ]; then
    certbot certonly --webroot -w /var/www/acme --non-interactive --agree-tos \
        --register-unsafely-without-email --cert-name "$domain" -d "$domain"
fi
install -m 0644 "$stage/nginx.conf" /etc/nginx/sites-available/node29.prestizh.site
nginx -t
systemctl reload nginx
install -m 0755 "$stage/renew-services.sh" /etc/letsencrypt/renewal-hooks/deploy/node29-services
docker compose -f /opt/remnanode/docker-compose.yml up -d
systemctl enable --now certbot.timer
echo 'TLS and node ready; only the new subdomain is configured.'
