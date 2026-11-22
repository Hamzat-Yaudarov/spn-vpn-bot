#!/bin/bash
# Fixed new-VPS deployment; never invokes DNS, certificate or lease APIs.
set -euo pipefail
umask 077
test "$(id -u)" = 0
ip -o -4 addr show scope global | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq 152.234.235.106
test "$(getent ahostsv4 panel.wayspn.online | awk '{print $1}' | sort -u)" = 144.31.170.147
stage=/root/vk-de-backend
test "$(stat -c '%u:%a' "$stage")" = 0:700
test ! -e /opt/remnanode
if docker inspect remnanode >/dev/null 2>&1; then exit 2; fi
for p in 2222 2443; do
    if ss -lntH "sport = :$p" | grep -q .; then echo 'Required port already occupied' >&2; exit 3; fi
done
for f in node.env docker-compose.yml validation.json; do
    test -f "$stage/$f" && test ! -L "$stage/$f"
    test "$(stat -c '%u:%a:%h' "$stage/$f")" = 0:600:1
done
image=remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e
docker run --rm --network none --entrypoint /usr/local/bin/xray -v "$stage/validation.json:/tmp/validate.json:ro" "$image" run -test -c /tmp/validate.json
ufw allow 22/tcp
ufw allow 2443/tcp
ufw allow from 144.31.170.147 to any port 2222 proto tcp
ufw default deny incoming
ufw --force enable
install -d -m 0700 /opt/remnanode
install -m 0600 "$stage/node.env" /opt/remnanode/node.env
install -m 0600 "$stage/docker-compose.yml" /opt/remnanode/docker-compose.yml
docker compose -f /opt/remnanode/docker-compose.yml up -d --pull never --no-build
echo 'Dedicated backend started; no customer-facing host was created.'
