#!/bin/bash
set -euo pipefail
stage="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "STOP: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'Run as root.'
python3 "$stage/prepare.py" check-stage "$stage"
[[ -f "$stage/installed-bootstrap" ]] || fail 'Run the clean-server bootstrap first.'
for resolver in 1.1.1.1 8.8.8.8; do
    [[ $(dig +time=3 +tries=1 +short "@$resolver" A de1.chislo.site) == 64.188.83.197 ]] \
        || fail 'Public A record does not exclusively match the DE1 origin.'
    [[ -z $(dig +time=3 +tries=1 +short "@$resolver" AAAA de1.chislo.site) ]] \
        || fail 'Unexpected AAAA record; IPv6 ownership/reachability needs separate review.'
done
if [[ ! -f /etc/letsencrypt/live/de1.chislo.site/fullchain.pem ]]; then
    certbot certonly --webroot -w /var/www/acme --non-interactive --agree-tos \
        --register-unsafely-without-email --cert-name de1.chislo.site -d de1.chislo.site
fi
install -m 0600 "$stage/nginx.conf" /etc/nginx/sites-available/de1.chislo.site
nginx -t
systemctl reload nginx
install -m 0755 "$stage/renew-nginx.sh" /etc/letsencrypt/renewal-hooks/deploy/de1-nginx
systemctl enable --now certbot.timer
echo 'DE1 TLS site and loopback XHTTP reverse-proxy route ready. No node or subscriber has been installed.'
