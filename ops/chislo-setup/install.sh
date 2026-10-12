#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
stage=/root/chislo-setup
test -f "$stage/node.env"
test -f "$stage/profile.json"
if [ ! -f "$stage/installed-bootstrap" ]; then
    apt-get update
    apt-get install -y docker.io docker-compose-v2 nginx certbot ufw
    install -d -m 0700 /opt/remnanode
    install -d -m 0755 /var/www/chislo /var/www/acme
    cp -a "$stage/site/." /var/www/chislo/
    chown -R root:root /var/www/chislo
    find /var/www/chislo -type d -exec chmod 0755 {} +
    find /var/www/chislo -type f -exec chmod 0644 {} +
    install -m 0600 "$stage/node.env" /opt/remnanode/node.env
    install -m 0644 "$stage/docker-compose.yml" /opt/remnanode/docker-compose.yml
    if [ -L /etc/nginx/sites-enabled/default ]; then
        cp -a /etc/nginx/sites-enabled/default "$stage/original-default-link"
        unlink /etc/nginx/sites-enabled/default
    fi
    install -m 0644 "$stage/bootstrap.conf" /etc/nginx/sites-available/chislo.site
    ln -sfn /etc/nginx/sites-available/chislo.site /etc/nginx/sites-enabled/chislo.site
    nginx -t
    systemctl enable --now docker nginx certbot.timer
    systemctl reload nginx
    ufw allow 22/tcp
    ufw allow 80/tcp
    ufw allow 443/tcp
    ufw allow 2083/tcp
    ufw allow 8443/udp
    ufw allow from 31.77.202.66 to any port 2222 proto tcp
    ufw default deny incoming
    ufw default allow outgoing
    ufw --force enable
    docker compose -f /opt/remnanode/docker-compose.yml pull
    touch "$stage/installed-bootstrap"
fi
echo 'HTTP site and firewall ready. Certificate requires DNS pointing to 31.76.50.87.'
