#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
stage="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "STOP: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail 'Run as root on the new DE1 VPS only.'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail 'Only clean Ubuntu 24.04 is supported.'
command -v python3 >/dev/null || fail 'Python 3 is required for private stage validation.'
command -v ip >/dev/null && command -v ss >/dev/null || fail 'iproute2 is required for origin/port checks.'
ip -4 -o address show | awk '$4 ~ /^64\.188\.83\.197\// { found=1 } END { exit !found }' \
    || fail 'Approved origin IPv4 is not assigned locally; review NAT/topology manually.'
python3 "$stage/prepare.py" check-stage "$stage"
[[ ! -e "$stage/installed-bootstrap" ]] || fail 'Bootstrap already ran; do not reuse this installer.'

if [[ -n ${SSH_CONNECTION:-} ]]; then
    read -r _ _ _ ssh_port <<< "$SSH_CONNECTION"
    [[ $ssh_port == 22 ]] || fail 'SSH must use port 22 before applying this firewall.'
fi

# Every destructive/install step is below these clean-server guards.
for binary in docker dockerd containerd nginx certbot; do
    ! command -v "$binary" >/dev/null || fail "Existing $binary installation detected."
done
for package in docker.io docker-ce docker-compose-v2 containerd containerd.io nginx nginx-common certbot; do
    status="$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)"
    [[ $status != 'install ok installed' ]] || fail "Existing package $package detected."
done
for path in /etc/docker /var/lib/docker /var/lib/containerd /opt/remnanode /etc/nginx /etc/letsencrypt /var/www/de1-chislo /var/www/acme; do
    [[ ! -e $path && ! -L $path ]] || fail "Existing deployment path: $path"
done
if ss -H -lntu | awk '$5 ~ /:(80|443|2222|10085)$/ { found=1 } END { exit !found }'; then
    fail 'A planned public/internal service port is already listening.'
fi
ufw_was_active=false
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active$'; then
    ufw_was_active=true
fi
for binary in iptables-save ip6tables-save; do
    if [[ $ufw_was_active == false ]] && command -v "$binary" >/dev/null && "$binary" | grep -q '^-A '; then
        fail 'Existing firewall rules detected; this is not a clean server.'
    fi
done

apt-get update
# Reject third-party package indexes; packages come from signed Ubuntu archives only.
apt-cache policy | awk '/^[[:space:]]+release / && !/a=now/ && !/(^|,)o=Ubuntu(,|$)/ { bad=1 } END { exit bad }' \
    || fail 'Non-Ubuntu APT origin detected.'
apt-get install -y --no-install-recommends docker.io docker-compose-v2 nginx certbot ufw dnsutils

install -d -m 0755 /var/www/de1-chislo /var/www/acme
cp -a "$stage/site/." /var/www/de1-chislo/
chown -R root:root /var/www/de1-chislo
find /var/www/de1-chislo -type d -exec chmod 0755 {} +
find /var/www/de1-chislo -type f -exec chmod 0644 {} +
if [[ -L /etc/nginx/sites-enabled/default ]]; then
    [[ $(readlink /etc/nginx/sites-enabled/default) == /etc/nginx/sites-available/default ]] \
        || fail 'Unexpected nginx default symlink.'
    unlink /etc/nginx/sites-enabled/default
fi
install -m 0644 "$stage/bootstrap.conf" /etc/nginx/sites-available/de1.chislo.site
ln -s /etc/nginx/sites-available/de1.chislo.site /etc/nginx/sites-enabled/de1.chislo.site
nginx -t
systemctl enable --now docker nginx certbot.timer
systemctl reload nginx

# Preserve the VPS's existing active UFW/OpenSSH policy; never reset its rules.
if [[ $ufw_was_active == false ]]; then
    ufw allow 22/tcp
    ufw default deny incoming
    ufw default allow outgoing
fi
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
# No node/control port or public proxy inbound is opened at this stage.
install -m 0600 /dev/null "$stage/installed-bootstrap"
echo 'DE1 HTTP/ACME site ready. Run finish.sh only after public DNS matches the approved origin.'
