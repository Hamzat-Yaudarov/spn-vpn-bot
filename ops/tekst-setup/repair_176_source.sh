#!/bin/bash
# Run only over the previous TEKST SSH pin at the verified migrated address.
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C
stage=/root/tekst176-route-20261108
target=/etc/network/interfaces
old=64.188.80.39
new=176.125.254.148
die() { printf 'Stopped: %s\n' "$1" >&2; exit 1; }
[[ $# = 1 && $1 = --apply && $(id -u) = 0 ]] || die 'root and --apply required'
[[ -d $stage && ! -L $stage && $(stat -c %u:%a "$stage") = 0:700 ]] || die 'unsafe stage'
for file in "$stage/interfaces.before" "$stage/interfaces.after"; do
    [[ -f $file && ! -L $file && $(stat -c %u:%a "$file") = 0:600 ]] || die 'unsafe candidate or backup'
done
[[ -f $target && ! -L $target && $(stat -c %u:%g:%a "$target") = 0:0:644 ]] || die 'unexpected interfaces metadata'
cmp -s "$target" "$stage/interfaces.before" || die 'interfaces changed since inspection'
sed -e "s/^    address   $old$/    address   $new/" \
    -e "s/^    post-up \/usr\/sbin\/ip -4 route change default via 10\.0\.0\.1 dev ens3 onlink src $old$/    post-up \/usr\/sbin\/ip -4 route change default via 10.0.0.1 dev ens3 onlink src $new/" \
    "$stage/interfaces.before" | cmp -s - "$stage/interfaces.after" || die 'unexpected candidate difference'
ip -o -4 addr show dev ens3 | grep -Fq " $new/32 " || die 'new source not assigned'
default=$(ip -o -4 route show default)
[[ $default != *$'\n'* ]] || die 'multiple default routes'
read -r -a route <<< "$default"
[[ ${route[*]} = 'default via 10.0.0.1 dev ens3 onlink' ]] || die 'unexpected default route'
ip -4 route get 8.8.8.8 | grep -Fq ' src 87.251.16.170 ' || die 'unexpected live source'
ifquery -i "$stage/interfaces.after" ens3:0 | grep -Fxq "address: $new" || die 'candidate ifupdown check failed'

changed=0
committed=0
tmp=''
rollback() {
    local code=$?
    if (( changed && ! committed )); then
        ip -4 route replace "${route[@]}" || true
        printf 'Restored the previous live route; persistent file not replaced\n' >&2
    fi
    if [[ -n $tmp && -f $tmp ]]; then rm -f -- "$tmp"; fi
    exit "$code"
}
trap rollback EXIT
ip -4 route change "${route[@]}" src "$new"
changed=1
ip -4 route get 8.8.8.8 | grep -Fq " src $new " || die 'new source not selected'
if ! timeout 6 getent ahostsv4 www.gstatic.com >/dev/null; then
    systemctl is-active --quiet systemd-resolved || die 'DNS unavailable'
    systemctl restart systemd-resolved
    timeout 8 getent ahostsv4 www.gstatic.com >/dev/null || die 'DNS still unavailable'
fi
result=$(curl --disable --noproxy '*' -4 -fsS --connect-timeout 5 --max-time 9 -o /dev/null -w '%{http_code} %{local_ip}' https://www.gstatic.com/generate_204)
[[ $result = "204 $new" ]] || die 'HTTPS egress test did not pass'
cmp -s "$target" "$stage/interfaces.before" || die 'concurrent persistent edit'
tmp=$(mktemp /etc/network/.interfaces.tekst176.XXXXXX)
install -o root -g root -m 0644 "$stage/interfaces.after" "$tmp"
mv -f -- "$tmp" "$target"
committed=1
printf 'HTTPS egress %s; alias and preferred source saved; no interface restart\n' "$result"
