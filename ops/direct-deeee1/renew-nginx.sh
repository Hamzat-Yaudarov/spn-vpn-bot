#!/bin/bash
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
# A global deploy hook must ignore certificates belonging to other services.
if [ "${RENEWED_LINEAGE:-}" != /etc/letsencrypt/live/deeee1.wayspn.com ]; then
    exit 0
fi
nginx -t
systemctl reload nginx
# Both transports use Nginx's certificate; no node restart is necessary.
