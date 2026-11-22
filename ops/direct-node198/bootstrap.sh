#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0
ip -o -4 addr show scope global | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq 179.236.145.198
domain=node198.wayspn.com
test "$(getent ahostsv4 "$domain" | awk '{print $1}' | sort -u)" = 179.236.145.198
test "$(getent ahostsv4 panel.wayspn.online | awk '{print $1}' | sort -u)" = 144.31.170.147
root=/var/www/direct-node198-acme
site=/etc/nginx/sites-available/$domain
link=/etc/nginx/sites-enabled/$domain
test ! -e "$site" && test ! -e "$link"
for enabled in /etc/nginx/sites-enabled/*; do
    test "$enabled" = /etc/nginx/sites-enabled/default
    test "$(readlink "$enabled")" = /etc/nginx/sites-available/default
done
install -d -m 0755 "$root"
install -m 0644 /root/direct-node198/bootstrap.conf "$site"
ln -s "$site" "$link"
mv /etc/nginx/sites-enabled/default /root/direct-node198/default.before
if ! nginx -t; then
    rm "$link"
    mv /root/direct-node198/default.before /etc/nginx/sites-enabled/default
    exit 1
fi
systemctl reload nginx
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 2083/tcp
ufw allow from 144.31.170.147 to any port 2222 proto tcp
ufw default deny incoming
ufw --force enable
certbot certonly --non-interactive --webroot -w "$root" --agree-tos --register-unsafely-without-email -d "$domain"
