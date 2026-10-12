#!/bin/bash
set -euo pipefail
stage=/root/chislo-setup
for domain in chislo.site www.chislo.site; do
    if ! dig @1.1.1.1 +short A "$domain" | grep -Fxq 31.76.50.87; then
        echo "DNS for $domain does not yet point to the VPS; refusing certificate issuance."
        exit 2
    fi
done
if [ ! -f /etc/letsencrypt/live/chislo.site/fullchain.pem ]; then
    certbot certonly --webroot -w /var/www/acme --non-interactive --agree-tos \
      --register-unsafely-without-email --cert-name chislo.site \
      -d chislo.site -d www.chislo.site
fi
install -m 0644 "$stage/chislo.site.conf" /etc/nginx/sites-available/chislo.site
nginx -t
systemctl reload nginx
install -m 0755 "$stage/renew-services.sh" /etc/letsencrypt/renewal-hooks/deploy/chislo-services
docker compose -f /opt/remnanode/docker-compose.yml up -d
systemctl enable --now certbot.timer
echo 'TLS and node installed; awaiting owner-created profile/node in Remnawave.'
