#!/usr/bin/env bash
# Runtime only. No account login, financial UI, controller config or timers.
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l

test "$(id -u)" = 0
test "$(cat /opt/wayspn/.controller-deployment)" = 276abbcb4f334b58ab25d9b2adc9d55c
test ! -L /opt/wayspn/venv
test ! -L /var/lib/spn-ip-failover/.cache
test "$(getent passwd spn-failover | cut -d: -f6)" = /var/lib/spn-ip-failover
exec 9>/run/spn-playwright-runtime-install.lock
flock -n 9

# Wait for package-manager locks, never kill unattended upgrades or remove locks.
apt_retry() {
    local attempt
    for attempt in 1 2 3 4 5; do
        if apt-get -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold "$@"; then
            return 0
        fi
        sleep 15
    done
    return 1
}
apt_retry update
# The pinned Playwright driver itself needs libatomic before install-deps can run.
apt_retry install -y --no-install-recommends python3-venv ca-certificates libatomic1

if test ! -x /opt/wayspn/venv/bin/python; then
    python3 -m venv /opt/wayspn/venv
fi
env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL -u PIP_TRUSTED_HOST \
    PIP_CONFIG_FILE=/dev/null /opt/wayspn/venv/bin/python -m pip install \
    --no-input --disable-pip-version-check --no-cache-dir --only-binary=:all: \
    --index-url https://pypi.org/simple 'playwright==1.65.1'

/opt/wayspn/venv/bin/python - <<'PY'
import importlib.metadata
import json
from pathlib import Path
import playwright
assert importlib.metadata.version('playwright') == '1.65.1'
manifest = json.loads((Path(playwright.__file__).parent / 'driver/package/browsers.json').read_text())
for name in ('chromium', 'chromium-headless-shell'):
    browser, = [item for item in manifest['browsers'] if item['name'] == name]
    assert browser['revision'] == '1254' and browser['browserVersion'] == '158.0.8127.18'
print('Pinned Playwright 1.65.1 / Chromium 1254 manifest verified')
PY

# The official dependency installer invokes apt-get itself. Give it the same
# bounded lock wait and preserve existing config files without global APT edits.
apt_config=$(mktemp /run/spn-playwright-apt.XXXXXX)
trap 'rm -f "$apt_config"' EXIT
printf '%s\n' 'DPkg::Lock::Timeout "600";' 'Dpkg::Options { "--force-confold"; };' > "$apt_config"
dependencies_ready=0
for attempt in 1 2 3 4 5; do
    if APT_CONFIG="$apt_config" /opt/wayspn/venv/bin/python -m playwright install-deps chromium; then
        dependencies_ready=1
        break
    fi
    sleep 15
done
test "$dependencies_ready" = 1

install -d -o spn-failover -g spn-failover -m 700 /var/lib/spn-ip-failover/.cache
runuser -u spn-failover -- env HOME=/var/lib/spn-ip-failover \
    PLAYWRIGHT_BROWSERS_PATH=/var/lib/spn-ip-failover/.cache/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000 \
    /opt/wayspn/venv/bin/python -m playwright install chromium

# Anonymous smoke test only. No saved profile, account, secret or paid operation.
runuser -u spn-failover -- env HOME=/var/lib/spn-ip-failover \
    PLAYWRIGHT_BROWSERS_PATH=/var/lib/spn-ip-failover/.cache/ms-playwright \
    /opt/wayspn/venv/bin/python - <<'PY'
import importlib.metadata
import json
import os
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

assert os.geteuid() != 0
with TemporaryDirectory(prefix='spn-browser-smoke-') as profile, sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        profile, headless=True, service_workers='block', accept_downloads=False,
    )
    try:
        context.route('**/*', lambda route: route.continue_()
                      if route.request.method in ('GET', 'HEAD', 'OPTIONS') else route.abort())
        page = context.pages[0] if context.pages else context.new_page()
        response = page.goto('https://bill.1cent.host/login', wait_until='domcontentloaded', timeout=30_000)
        location = urlsplit(page.url)
        assert response is not None and response.ok
        assert location.scheme == 'https' and location.hostname == 'bill.1cent.host'
        assert location.path == '/login'
        assert context.service_workers == []
        product = context.new_cdp_session(page).send('Browser.getVersion')['product']
        assert product.endswith('/158.0.8127.18')
        print(json.dumps({'playwright': importlib.metadata.version('playwright'),
                          'browser': p.chromium.executable_path,
                          'browser_version': product,
                          'uid': os.geteuid(), 'service_workers': 'block',
                          'anonymous_login_get_status': response.status,
                          'account_login_attempted': False, 'paid_operations_enabled': False}))
    finally:
        context.close()
PY
