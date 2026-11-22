#!/bin/bash
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
[ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/ch121.wayspn.com ] || exit 0
nginx -t
systemctl reload nginx
