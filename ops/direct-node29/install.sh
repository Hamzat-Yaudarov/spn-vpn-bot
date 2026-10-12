#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
stage=/root/direct-node29
test -s "$stage/node.env"
test -s "$stage/profile.json"
if [ ! -f "$stage/installed-bootstrap" ]; then
    apt-get update
    apt-get install -y docker.io docker-compose-v2 nginx certbot ufw dnsutils
    install -d -m 0700 /opt/remnanode
    install -d -m 0755 /var/www/node29 /var/www/acme
    install -m 0644 "$stage/index.html" /var/www/node29/index.html
    install -m 0600 "$stage/node.env" /opt/remnanode/node.env
    install -m 0644 "$stage/docker-compose.yml" /opt/remnanode/docker-compose.yml
    if [ -L /etc/nginx/sites-enabled/default ]; then
        cp -a /etc/nginx/sites-enabled/default "$stage/original-default-link"
        unlink /etc/nginx/sites-enabled/default
    fi
    install -m 0644 "$stage/bootstrap.conf" /etc/nginx/sites-available/node29.prestizh.site
    ln -sfn /etc/nginx/sites-available/node29.prestizh.site /etc/nginx/sites-enabled/node29.prestizh.site
    nginx -t
    systemctl enable --now docker nginx certbot.timer
    systemctl reload nginx
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw allow 2083/tcp
    ufw allow 8443/udp
    ufw allow from 144.31.170.147 to any port 2222 proto tcp
    ufw default deny incoming
    ufw default allow outgoing
    ufw --force enable
    docker compose -f /opt/remnanode/docker-compose.yml pull
    touch "$stage/installed-bootstrap"
fi
echo 'Bootstrap installed; ready for node29-only certificate issuance.'
