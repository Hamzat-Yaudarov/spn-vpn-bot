#!/bin/bash
# Only for the verified 1CENT node serving Estonia 1 and 2.
set -euo pipefail
umask 077
export LC_ALL=C PATH=/usr/sbin:/usr/bin:/sbin:/bin
old=31.59.218.221
removed=31.59.218.234
new=95.85.242.156
cfg=/etc/network/interfaces
backup=/root/spn-ee156-route-20261108
old_line='    post-up /usr/sbin/ip -4 route change default via 10.0.0.1 dev ens3 onlink src 31.59.218.234'
die() { printf 'Stopped: %s\n' "$1" >&2; exit 1; }

[[ $# = 1 && $1 = --apply && $(id -u) = 0 ]] || die 'root and --apply required'
[[ $(hostname -s) = vds100269 ]] || die 'wrong VPS'
[[ -f $cfg && ! -L $cfg && $(stat -c '%u:%g:%a:%h' "$cfg") = 0:0:644:1 ]] || die 'unexpected interface config metadata'
[[ ! -e $backup && ! -L $backup ]] || die 'backup path already exists; inspect before retry'
ip -o -4 addr show dev ens3 | grep -Fq " $new/32 " || die 'preferred source is not assigned'
if ip -o -4 addr show dev ens3 | grep -Fq " $removed/32 "; then die 'previous alias is still active'; fi
default=$(ip -o -4 route show default)
[[ $default != *$'\n'* ]] || die 'multiple default routes'
read -r -a route <<< "$default"
[[ ${route[*]} = 'default via 10.0.0.1 dev ens3 onlink' ]] || die 'unexpected default route'
ip -4 route get 1.1.1.1 | grep -Fq " src $old " || die 'unexpected selected source'
ifquery ens3:0 | grep -Fxq "address: $removed" || die 'ifupdown alias changed'

panel_probe() {
    curl -q --noproxy '*' "$@" -4 --resolve panel.wayspn.online:443:144.31.170.147 \
        --connect-timeout 4 --max-time 7 -fsS -o /dev/null -w '%{http_code} %{local_ip}' https://panel.wayspn.online/
}
[[ $(panel_probe --interface "$new") = "200 $new" ]] || die 'explicit source probe did not pass'
if panel_probe >/dev/null 2>&1; then die 'default route works now; no change applied'; fi

install -d -m 0700 "$backup"
install -m 0600 "$cfg" "$backup/interfaces.before"
printf '%s\n' "$default" > "$backup/default.before"
awk -v old_line="$old_line" -v removed="$removed" -v new="$new" '
    /^iface[[:space:]]/ { alias=($0=="iface ens3:0 inet static"); if (alias) aliases++ }
    alias && $0=="    address   " removed { print "    address   " new; addresses++; next }
    alias && $0==old_line { sub(/31[.]59[.]218[.]234$/,new); print; sources++; next }
    /route[[:space:]].*default.*[[:space:]]src[[:space:]]/ { other_sources++ }
    { print }
    END { if (aliases!=1 || addresses!=1 || sources!=1 || other_sources) exit 1 }
' "$backup/interfaces.before" > "$backup/interfaces.after" || die 'unexpected alias layout or preferred source'
ifquery -i "$backup/interfaces.after" ens3:0 | grep -Fxq "address: $new" || die 'candidate failed ifupdown check'
cmp -s "$cfg" "$backup/interfaces.before" || die 'concurrent interface edit'

changed=0
committed=0
tmp=''
rollback() {
    local code=$?
    if (( changed && ! committed )); then
        if ip -4 route replace "${route[@]}"; then
            printf 'Restored previous live route; persistent config was not replaced\n' >&2
        else
            printf 'Automatic route rollback failed; check provider console\n' >&2
        fi
    fi
    if [[ -n $tmp && -f $tmp ]]; then rm -f -- "$tmp"; fi
    exit "$code"
}
trap rollback EXIT
ip -4 route change "${route[@]}" src "$new"
changed=1
ip -4 route get 1.1.1.1 | grep -Fq " src $new " || die 'new source not selected'
[[ $(panel_probe) = "200 $new" ]] || die 'panel egress did not recover'
if ! timeout 8 getent ahostsv4 www.gstatic.com >/dev/null; then
    resolvectl flush-caches || true
    timeout 8 getent ahostsv4 www.gstatic.com >/dev/null || die 'DNS did not recover'
fi
result=$(curl -q --noproxy '*' -4 --connect-timeout 5 --max-time 9 -fsS -o /dev/null \
    -w '%{http_code} %{local_ip}' https://www.gstatic.com/generate_204)
[[ $result = "204 $new" ]] || die 'independent HTTPS egress did not recover'
cmp -s "$cfg" "$backup/interfaces.before" || die 'concurrent interface edit'
tmp=$(mktemp /etc/network/.interfaces.ee156.XXXXXX)
install -o root -g root -m 0644 "$backup/interfaces.after" "$tmp"
mv -f -- "$tmp" "$cfg"
committed=1
printf 'DNS and HTTPS %s; preferred source saved, other addresses unchanged; no restart\n' "$result"
printf 'Private backup: %s\n' "$backup"
