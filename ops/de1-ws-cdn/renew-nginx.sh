#!/bin/bash
set -euo pipefail
nginx -t
systemctl reload nginx
