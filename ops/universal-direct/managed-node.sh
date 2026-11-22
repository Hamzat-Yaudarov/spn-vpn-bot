#!/bin/bash
# Called only by deploy.py over an authenticated SSH connection.
set -euo pipefail
umask 077
export LC_ALL=C
ACTION=${1:?}; DOMAIN=${2:?}; NODE_IP=${3:?}; PANEL_IP=${4:?}; SSH_PORT=${5:?}; DEPLOY_ID=${6:?}
[[ "$DEPLOY_ID" =~ ^[0-9a-f]{32}$ ]] || exit 2
[[ "$DOMAIN" =~ ^[a-z0-9.-]+$ ]] || exit 2
[[ "$NODE_IP" =~ ^[0-9.]+$ && "$PANEL_IP" =~ ^[0-9.]+$ && "$SSH_PORT" =~ ^[0-9]+$ ]] || exit 2
STAGE=/root/spn-direct-managed-$DEPLOY_ID
SITE=/etc/nginx/sites-available/spn-direct.conf
ENABLED=/etc/nginx/sites-enabled/spn-direct.conf
NODE=/opt/remnanode
IMAGE=remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e
fail() { printf 'Stopped: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail 'Root is required'
. /etc/os-release
[ "$ID:$VERSION_ID" = ubuntu:24.04 ] || fail 'Clean Ubuntu 24.04 is required'
[ -d "$STAGE" ] && [ ! -L "$STAGE" ] && [ "$(stat -c '%u:%a' "$STAGE")" = 0:700 ] || fail 'Unsafe staging directory'
ip -o -4 addr show scope global | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq "$NODE_IP" || fail 'Target IPv4 is not assigned to this VPS'

owned_node() {
    [ -d "$NODE" ] && [ ! -L "$NODE" ] && [ -f "$NODE/.spn-deployment" ] \
        && [ ! -L "$NODE/.spn-deployment" ] && [ "$(cat "$NODE/.spn-deployment")" = "$DEPLOY_ID" ]
}
safe_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u:%a:%h' "$1")" = 0:600:1 ] || fail 'Unsafe staged file'
}
install_owned() {
    local source=$1 destination=$2 mode=$3
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        [ -f "$destination" ] && [ ! -L "$destination" ] && cmp -s "$source" "$destination" || fail "Existing file differs: $destination"
        chmod "$mode" "$destination"
    else
        install -m "$mode" "$source" "$destination"
    fi
}
check_container() {
    if command -v docker >/dev/null && docker inspect remnanode >/dev/null 2>&1; then
        [ "$(docker inspect -f '{{index .Config.Labels "spn.deploy.id"}}' remnanode)" = "$DEPLOY_ID" ] || fail 'Unrelated remnanode container exists'
        [ "$(docker inspect -f '{{.Config.Image}}|{{.HostConfig.NetworkMode}}' remnanode)" = "$IMAGE|host" ] || fail 'Owned container configuration changed'
    fi
}
check_sites() {
    shopt -s nullglob
    local p
    for p in /etc/nginx/sites-enabled/*; do
        if [ "$p" = "$ENABLED" ]; then
            [ -L "$p" ] && [ "$(readlink "$p")" = "$SITE" ] && owned_node || fail 'Unowned Nginx site'
        else
            [ "$p" = /etc/nginx/sites-enabled/default ] && [ -L "$p" ] \
                && [ "$(readlink "$p")" = /etc/nginx/sites-available/default ] || fail 'Custom Nginx site exists'
        fi
    done
}

panel_rule_present() {
    # ufw status may append comments and spell the action ALLOW or ALLOW IN.
    # Compare exact fields, not an IP interpolated into a regular expression.
    awk -v source="$PANEL_IP" '
        { sub(/[[:space:]]*#.*/, "") }
        $1 == "2222/tcp" && $2 == "ALLOW" {
            field = ($3 == "IN" ? 4 : 3)
            if (NF != field) next
            address = $field
            sub(/\/32$/, "", address)
            if (address == source) found = 1
        }
        END { exit !found }
    '
}

case "$ACTION" in
preflight)
    check_sites
    if [ -e "$NODE" ] || [ -L "$NODE" ]; then owned_node || fail 'Existing node installation is not ours'; fi
    check_container
    if [ ! -f "$STAGE/preflight-ok" ]; then
        for p in 80 443 2222 10087 10088; do
            [ -z "$(ss -H -lnt "sport = :$p")" ] || fail "TCP$p is occupied"
        done
        [ ! -e "$SITE" ] && [ ! -L "$SITE" ] || fail 'Nginx site already exists'
        if command -v ufw >/dev/null && ufw status | grep -Fxq 'Status: active'; then fail 'Existing active firewall'; fi
        printf '%s\n' "$DOMAIN|$NODE_IP|$PANEL_IP|$SSH_PORT" > "$STAGE/preflight-ok"
    else
        [ "$(cat "$STAGE/preflight-ok")" = "$DOMAIN|$NODE_IP|$PANEL_IP|$SSH_PORT" ] || fail 'Saved target parameters differ'
    fi
    printf 'VPS preflight OK\n'
    ;;
install)
    [ -f "$STAGE/preflight-ok" ] || fail 'Preflight is required'
    for p in node.env docker-compose.yml bootstrap.conf nginx.conf renew-nginx.sh; do safe_file "$STAGE/$p"; done
    check_sites
    if [ -e "$NODE" ] || [ -L "$NODE" ]; then owned_node || fail 'Unowned existing node'; fi
    check_container
    if [ ! -f "$STAGE/packages-ok" ]; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y ca-certificates docker.io docker-compose-v2 nginx certbot ufw
        systemctl enable --now docker nginx
        docker compose version >/dev/null
        touch "$STAGE/packages-ok"
    fi
    if [ ! -e "$NODE" ]; then
        # Prepare marker before atomically claiming the new directory. A crash
        # cannot leave an unowned /opt/remnanode that prevents safe resumption.
        claim=$(mktemp -d /opt/.spn-direct-claim.XXXXXXXX)
        printf '%s\n' "$DEPLOY_ID" > "$claim/.spn-deployment"
        mv -Tn "$claim" "$NODE"
        if [ -d "$claim" ]; then rm "$claim/.spn-deployment"; rmdir "$claim"; fi
    fi
    owned_node || fail 'Ownership marker differs'
    install_owned "$STAGE/node.env" "$NODE/node.env" 0600
    install_owned "$STAGE/docker-compose.yml" "$NODE/docker-compose.yml" 0600
    if [ -e "$SITE" ] || [ -L "$SITE" ]; then
        [ ! -L "$SITE" ] && { cmp -s "$SITE" "$STAGE/bootstrap.conf" || cmp -s "$SITE" "$STAGE/nginx.conf"; } || fail 'Nginx site was edited'
    else
        install -m 0644 "$STAGE/bootstrap.conf" "$SITE"
    fi
    if [ -L /etc/nginx/sites-enabled/default ]; then
        [ ! -e "$STAGE/nginx-default.before" ] && [ ! -L "$STAGE/nginx-default.before" ] || fail 'Default-site backup already exists'
        mv /etc/nginx/sites-enabled/default "$STAGE/nginx-default.before"
    fi
    if [ ! -L "$ENABLED" ]; then [ ! -e "$ENABLED" ] || fail 'Unexpected Nginx link'; ln -s "$SITE" "$ENABLED"; fi
    install -d -m 0755 /var/www/spn-direct-acme
    nginx -t
    systemctl reload nginx
    if [ ! -f "$STAGE/firewall-ok" ]; then
        ufw status verbose > "$STAGE/firewall.before"
        ufw allow "$SSH_PORT/tcp" comment SPN-SSH
        ufw allow 80/tcp comment SPN-ACME
        ufw allow 443/tcp comment SPN-TLS
        ufw allow from "$PANEL_IP" to any port 2222 proto tcp comment SPN-PANEL
        ufw default deny incoming
        ufw default allow outgoing
        ufw --force enable
        touch "$STAGE/firewall-ok"
    fi
    cert=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
    if [ ! -s "$cert" ] || ! openssl x509 -in "$cert" -noout -checkend 86400 >/dev/null; then
        certbot certonly --webroot -w /var/www/spn-direct-acme -d "$DOMAIN" \
            --cert-name "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring
    fi
    openssl x509 -in "$cert" -noout -checkhost "$DOMAIN" >/dev/null
    install -m 0644 "$STAGE/nginx.conf" "$SITE"
    if ! nginx -t; then
        install -m 0644 "$STAGE/bootstrap.conf" "$SITE"
        fail 'TLS config failed; HTTP config restored'
    fi
    install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
    install_owned "$STAGE/renew-nginx.sh" /etc/letsencrypt/renewal-hooks/deploy/spn-direct-nginx 0755
    systemctl enable --now certbot.timer
    systemctl reload nginx
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then docker pull "$IMAGE"; fi
    docker compose -f "$NODE/docker-compose.yml" up -d --pull never
    touch "$STAGE/install-ok"
    printf 'Managed VPS installation ready\n'
    ;;
verify)
    owned_node || fail 'Missing deployment ownership marker'
    check_container
    [ "$(docker inspect -f '{{.State.Running}}' remnanode)" = true ] || fail 'Node container is stopped'
    systemctl is-active --quiet nginx
    nginx -t
    for p in 2222 443 10087 10088; do
        [ -n "$(ss -H -lnt "sport = :$p")" ] || fail "TCP$p is not listening"
    done
    for p in 10087 10088; do
        ss -H -lnt "sport = :$p" | awk '{print $4}' | grep -Fxq "127.0.0.1:$p" || fail 'Transport backend is not loopback'
        [ "$(ss -H -lnt "sport = :$p" | wc -l)" -eq 1 ] || fail 'Unexpected additional transport listener'
    done
    ufw status | grep -Fxq 'Status: active' || fail 'Firewall is not active'
    ufw status | panel_rule_present || fail 'Panel firewall rule is missing'
    printf 'Node listeners, TLS configuration and firewall OK\n'
    ;;
*) exit 2 ;;
esac
