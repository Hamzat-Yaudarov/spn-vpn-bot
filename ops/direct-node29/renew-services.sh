#!/bin/bash
set -euo pipefail
nginx -t
systemctl reload nginx
if docker inspect remnanode >/dev/null 2>&1; then
    docker restart remnanode >/dev/null
fi
