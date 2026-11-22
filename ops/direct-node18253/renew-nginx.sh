#!/bin/bash
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
[ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/node18253.wayspn.com ] || exit 0
nginx -t
systemctl reload nginx
