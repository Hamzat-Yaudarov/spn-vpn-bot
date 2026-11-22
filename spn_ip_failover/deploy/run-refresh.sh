#!/bin/sh
set -eu
credentials=${CREDENTIALS_DIRECTORY:?systemd credentials directory is required}
REMNAWAVE_API_TOKEN=$(cat "$credentials/remnawave")
export REMNAWAVE_API_TOKEN
exec /usr/bin/python3 -B -m spn_ip_failover.refresh --config /var/lib/spn-ip-failover/refresh.json
