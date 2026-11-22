#!/usr/bin/env bash
# Owner explicitly approved retiring all old applications on this VPS.
# Keeps Ubuntu, SSH/authentication and OS/network services; never an OS reinstall.
# Fixed target, fixed old node, local rollback archive; no credential output.
set -euo pipefail
umask 077

if [[ "${1:-}" != "--apply-owner-approved-cleanup" || "$EUID" -ne 0 ]]; then
  printf '%s\n' 'Requires root and --apply-owner-approved-cleanup on the confirmed VPS.' >&2
  exit 2
fi
read -r client_ip client_port server_ip server_port <<< "${SSH_CONNECTION:-}"
if [[ "$server_ip" != '92.42.102.103' ]]; then
  printf '%s\n' 'Wrong SSH target; no changes made.' >&2
  exit 2
fi
backup='/var/backups/spn-franchise-before-clean-20261021'
if [[ -e "$backup" || -e /opt/spn-franchise-staging ]]; then
  printf '%s\n' 'Prior preparation exists; inspect it instead of repeating cleanup.' >&2
  exit 2
fi
if [[ "$(docker inspect --format '{{.Config.Image}}' remnanode)" != 'remnawave/node:latest' ]]; then
  printf '%s\n' 'Old-node identity changed; no changes made.' >&2
  exit 2
fi
if [[ "$(docker ps -aq | wc -l)" -ne 1 ]]; then
  printf '%s\n' 'Unexpected containers; no changes made.' >&2
  exit 2
fi

install -d -m 0700 "$backup"
items=(etc/nginx opt/remnanode)
[[ ! -d /etc/letsencrypt ]] || items+=(etc/letsencrypt)
tar -C / -czf "$backup/old-configs.tar.gz" "${items[@]}"
chmod 0600 "$backup/old-configs.tar.gz"
tar -tzf "$backup/old-configs.tar.gz" >/dev/null

systemctl stop nginx
systemctl disable --now certbot.timer
docker rm -f remnanode
docker image rm remnawave/node:latest
systemctl disable --now docker.service docker.socket containerd.service
rm -rf -- /opt/remnanode /root/installer /var/www/html
rm -f -- /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf
# Old renewal configurations are inactive; the private rollback copy retains
# their certificates/account data. Do not issue/agree to new certificates here.
if [[ -d /etc/letsencrypt/renewal ]]; then
  mv /etc/letsencrypt/renewal "$backup/old-renewal"
  install -d -m 0700 /etc/letsencrypt/renewal
fi

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv ca-certificates nginx certbot python3-certbot-nginx

if ! id spn-partner >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/spn-partner --shell /usr/sbin/nologin spn-partner
fi
install -d -o root -g spn-partner -m 0750 /opt/spn-franchise-staging /opt/spn-franchise-staging/releases
install -d -o root -g root -m 0700 /etc/spn-partner-private
install -d -o spn-partner -g spn-partner -m 0700 /var/lib/spn-partner

cat > /etc/nginx/sites-enabled/00-franchise-closed.conf <<'NGINX'
# No unconfigured backend or legacy VPN is exposed while setup is incomplete.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    access_log off;
    server_tokens off;
    return 444;
}
NGINX
nginx -t
systemctl start nginx
systemctl reset-failed certbot.service || true
printf '%s\n' 'Old node retired; OS and SSH preserved. Private rollback archive retained.'
printf '%s\n' 'Staging directories prepared. No partner service, credentials, billing or webhook enabled.'
ss -lntup
