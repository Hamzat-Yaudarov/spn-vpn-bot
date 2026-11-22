#!/bin/bash
set -euo pipefail
. /root/vk-rehome-de177/common.sh

safe_file "$stage/bootstrap.conf"
for path in "$site" "$enabled" "$default_backup" "$webroot"; do absent "$path"; done
for path in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
    [[ -e "$path" || -L "$path" ]] || continue
    [[ "$path" == "$default_site" && -L "$path" && "$(readlink "$path")" == /etc/nginx/sites-available/default ]] || fail 'unexpected enabled Nginx configuration'
done
free_port 443
listeners=$(ss -H -lntp 'sport = :80')
if [[ -n "$listeners" ]]; then
    [[ -L "$default_site" ]] || fail 'TCP 80 is used without the distribution default site'
    ! grep -vF '("nginx"' <<< "$listeners" || fail 'TCP 80 is used by a service other than Nginx'
fi
nginx -t
systemctl is-active --quiet nginx
made_root=0
made_site=0
made_link=0
rollback() {
    local code=$?
    trap - EXIT
    if (( code )); then
        if (( made_link )); then rm -f -- "$enabled"; fi
        if (( made_site )); then rm -f -- "$site"; fi
        if (( made_root )); then rmdir -- "$webroot" 2>/dev/null || true; fi
        if [[ -L "$default_backup" ]]; then mv -- "$default_backup" "$default_site"; fi
        if nginx -t; then systemctl reload nginx || true; fi
        echo 'HTTP bootstrap failed; rollback attempted. Inspect before retry.' >&2
    fi
    exit "$code"
}
trap rollback EXIT
install -d -m 0755 "$webroot"
made_root=1
made_site=1
install -m 0644 "$stage/bootstrap.conf" "$site"
if [[ -L "$default_site" ]]; then mv -- "$default_site" "$default_backup"; fi
ln -s -- "$site" "$enabled"
made_link=1
nginx -t
systemctl reload nginx
status=$(curl --silent --show-error --max-time 5 --noproxy '*' --resolve "$domain:80:$address" \
    --output /dev/null --write-out '%{http_code}' "http://$domain/health")
[[ "$status" == 503 ]] || fail 'unexpected HTTP bootstrap response'
trap - EXIT
echo 'HTTP bootstrap ready. No certificate requested and no node changes.'
