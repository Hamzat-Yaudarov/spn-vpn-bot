#!/bin/sh
set -eu
credentials=${CREDENTIALS_DIRECTORY:?systemd credentials directory is required}
REMNAWAVE_API_TOKEN=$(cat "$credentials/remnawave")
ONECENT_EMAIL=$(cat "$credentials/onecent_email")
ONECENT_PASSWORD=$(cat "$credentials/onecent_password")
export REMNAWAVE_API_TOKEN ONECENT_EMAIL ONECENT_PASSWORD
case "${1-}" in
  preflight)
    /opt/wayspn/venv/bin/python -B -m spn_ip_failover.refresh --config /var/lib/spn-ip-failover/refresh.json
    exec /opt/wayspn/venv/bin/python -B -m spn_ip_failover.five_runtime preflight
    ;;
  readiness)
    /opt/wayspn/venv/bin/python -B -m spn_ip_failover.refresh --config /var/lib/spn-ip-failover/refresh.json
    exec /opt/wayspn/venv/bin/python -B -m spn_ip_failover.five_runtime preflight --readiness-only
    ;;
  run)
    exec /opt/wayspn/venv/bin/python -B -m spn_ip_failover --config /var/lib/spn-ip-failover/rotation.json --state /var/lib/spn-ip-failover/rotation run --apply --watch
    ;;
  *) exit 2 ;;
esac
