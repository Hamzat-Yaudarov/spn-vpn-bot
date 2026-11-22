#!/bin/bash
# Root bootstrap for the separately selected RF checker; no hosting purchases.
set -euo pipefail
umask 077
export LC_ALL=C
export DEBIAN_FRONTEND=noninteractive
DEPLOYMENT=${1:?deployment identity required}
[[ "$DEPLOYMENT" =~ ^[0-9a-f]{32}$ ]] || exit 2
[ "$(id -u)" = 0 ]
. /etc/os-release
[ "$ID:$VERSION_ID" = ubuntu:24.04 ]
[ "$(uname -m)" = x86_64 ]
MARKER=/opt/wayspn/.controller-deployment
if [ -e /opt/wayspn ]; then
    [ ! -L /opt/wayspn ] && [ -f "$MARKER" ] && [ ! -L "$MARKER" ]
    [ "$(stat -c '%u:%a:%h' "$MARKER")" = 0:600:1 ]
    [ "$(cat "$MARKER")" = "$DEPLOYMENT" ]
else
    ! id spn-failover >/dev/null 2>&1
    install -d -m 755 /opt/wayspn
    printf '%s\n' "$DEPLOYMENT" > "$MARKER"
    chmod 600 "$MARKER"
fi
for ((attempt=0; attempt<=120; attempt++)); do
    if output=$(apt-get -o DPkg::Lock::Timeout=600 update 2>&1); then
        printf '%s\n' "$output"
        break
    else status=$?; fi
    if [[ "$output" != *"Could not get lock"* && "$output" != *"Unable to acquire"* ]] || [ "$attempt" = 120 ]; then
        printf '%s\n' "$output" >&2
        exit "$status"
    fi
    sleep 5
done
apt-get -o DPkg::Lock::Timeout=600 install -y ca-certificates curl python3
if ! id spn-failover >/dev/null 2>&1; then
    useradd --system --user-group --create-home --home-dir /var/lib/spn-ip-failover --shell /bin/sh spn-failover
fi
install -d -o spn-failover -g spn-failover -m 700 /var/lib/spn-ip-failover
install -d -o root -g spn-failover -m 750 /etc/spn-ip-failover
install -d -o root -g root -m 700 /etc/spn-ip-failover/credentials
install -d -o root -g root -m 755 /opt/xray/26.7.28
ENGINE=/opt/xray/26.7.28/xray
SHA=64d46afb80adea1bf97a0d467e83f4a9ac1ebd0995891e84bca3f1a1d1affb1d
if [ ! -e "$ENGINE" ]; then
    TMP=$(mktemp -d /root/spn-xray-XXXXXXXX)
    trap 'rm -f "$TMP/xray.zip"; rmdir "$TMP" 2>/dev/null || true' EXIT
    curl --proto '=https' --proto-redir '=https' --fail --location --connect-timeout 15 \
        --max-time 240 --max-filesize 100000000 \
        https://github.com/XTLS/Xray-core/releases/download/v26.7.28/Xray-linux-64.zip -o "$TMP/xray.zip"
    python3 - "$TMP/xray.zip" "$ENGINE" "$SHA" <<'PY'
import hashlib,os,sys,zipfile
from pathlib import Path
archive,target,expected=sys.argv[1:]
path=Path(target)
assert not path.exists() and not path.is_symlink()
with zipfile.ZipFile(archive) as z:
    records=[x for x in z.infolist() if x.filename=='xray']
    assert len(records)==1 and 0<records[0].file_size<80000000
    data=z.read(records[0])
assert hashlib.sha256(data).hexdigest()==expected
fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o755)
os.fchmod(fd,0o755)
with os.fdopen(fd,'wb') as output:
    output.write(data);output.flush();os.fsync(output.fileno())
PY
fi
[ ! -L "$ENGINE" ]
case "$(stat -c '%u:%a:%h' "$ENGINE")" in 0:700:1|0:755:1) ;; *) exit 2 ;; esac
printf '%s  %s\n' "$SHA" "$ENGINE" | sha256sum --check --status
chmod 755 "$ENGINE"
"$ENGINE" version
printf 'RF checker packages and pinned engine ready; paid automation is not enabled.\n'
