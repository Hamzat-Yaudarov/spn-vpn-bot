#!/bin/bash
# Start only the dedicated DE1 node. The API is restricted to the panel IP.
set -euo pipefail
stage="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fail() { echo "STOP: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail 'Run as root.'
[[ -f "$stage/installed-bootstrap" ]] || fail 'Origin bootstrap missing.'
[[ -f /etc/letsencrypt/live/de1.chislo.site/fullchain.pem ]] || fail 'Origin TLS missing.'
[[ ! -e /opt/remnanode ]] || fail 'Existing node deployment requires manual review.'
[[ $(stat -c %a "$stage/node.env") == 600 ]] || fail 'Private node.env mode required.'
[[ $(stat -c %a "$stage/docker-compose.yml") == 600 ]] || fail 'Private compose mode required.'
python3 - "$stage/node.env" <<'PY'
import pathlib, sys
lines = pathlib.Path(sys.argv[1]).read_text().splitlines()
assert len(lines) == 2 and lines[0] == 'NODE_PORT=2222'
assert lines[1].startswith('SECRET_KEY=')
secret = lines[1].removeprefix('SECRET_KEY=')
assert len(secret) > 20 and not any(c.isspace() for c in secret)
PY
ip -4 addr show | grep -q 'inet 64\.188\.83\.197/' || fail 'Wrong VPS.'
ufw status | grep -q '^Status: active' || fail 'Firewall must be active.'
if ufw status | grep -q '^2222.*Anywhere'; then
    fail 'Unexpected public node API firewall rule.'
fi
for port in 2222 10085; do
    [[ -z $(ss -H -lnt "sport = :$port") ]] || fail "Port $port already in use."
done
image='remnawave/node@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e'
docker image inspect "$image" >/dev/null
grep -Fq "image: $image" "$stage/docker-compose.yml" || fail 'Unexpected node image.'
install -d -m 0700 /opt/remnanode
install -m 0600 "$stage/node.env" /opt/remnanode/node.env
install -m 0600 "$stage/docker-compose.yml" /opt/remnanode/docker-compose.yml
ufw allow proto tcp from 144.31.170.147 to any port 2222 comment 'Remnawave panel DE1 only'
docker compose --project-directory /opt/remnanode up -d
docker compose --project-directory /opt/remnanode ps
echo 'Dedicated node started; node API TCP 2222 allowed only from the confirmed panel IP.'
