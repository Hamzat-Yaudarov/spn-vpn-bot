#!/bin/bash
# Invoked only over an authenticated SSH session by the local installer.
set -euo pipefail
umask 077
export LC_ALL=C

ACTION=${1:?}; NODE_IP=${2:?}; PANEL_IP=${3:?}; SSH_PORT=${4:?}; DEPLOY_ID=${5:?}
# Older immutable plans pass only their domain. New plans explicitly opt in.
SITE_VERSION=0; CERT_DOMAIN=${6:?}
if [ "$#" -eq 7 ]; then SITE_VERSION=$6; CERT_DOMAIN=$7; fi
IMAGE=remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e
STAGE=/root/spn-reality-managed-$DEPLOY_ID
NODE=/opt/spn-reality-node
SITE=/var/lib/spn-tls-site-$DEPLOY_ID
SITE_SERVICE=spn-tls-site-$DEPLOY_ID.service
SITE_UNIT=/etc/systemd/system/$SITE_SERVICE

fail() { printf 'Stopped: %s\n' "$*" >&2; exit 1; }
apt_install() {
    # Ubuntu may start unattended-upgrades immediately after provisioning.
    # Let APT itself wait on its fcntl/dpkg locks; never remove or kill them.
    apt-get -o DPkg::Lock::Timeout=600 install -y "$@"
}
apt_update() {
    local attempt status output
    # DPkg::Lock::Timeout does not reliably cover the lists lock used by update.
    # Retry ONLY that explicit lock error, preserving all other APT failures.
    for ((attempt=0; attempt<=120; attempt++)); do
        if output=$(apt-get -o DPkg::Lock::Timeout=600 update 2>&1); then
            printf '%s\n' "$output"
            return 0
        else
            status=$?
        fi
        if [[ "$output" != *"Could not get lock"* && "$output" != *"Unable to acquire"* ]]; then
            printf '%s\n' "$output" >&2
            return "$status"
        fi
        if [ "$attempt" -eq 120 ]; then
            printf '%s\n' "$output" >&2
            return "$status"
        fi
        if [ "$attempt" -eq 0 ]; then printf 'Ubuntu is updating packages; waiting for the APT lock (up to 10 minutes)…\n'; fi
        sleep 5
    done
}
valid_ipv4() {
    local address=$1 a b c d extra octet
    [[ "$address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    IFS=. read -r a b c d extra <<< "$address"
    [ -z "${extra:-}" ] || return 1
    for octet in "$a" "$b" "$c" "$d"; do
        [[ "$octet" == 0 || "$octet" =~ ^[1-9][0-9]{0,2}$ ]] || return 1
        (( 10#$octet <= 255 )) || return 1
    done
}
[[ "$DEPLOY_ID" =~ ^[0-9a-f]{32}$ ]] || fail 'Invalid deployment ID'
valid_ipv4 "$NODE_IP" && valid_ipv4 "$PANEL_IP" || fail 'Invalid IPv4'
[[ "$SSH_PORT" =~ ^[1-9][0-9]{0,4}$ ]] && (( SSH_PORT <= 65535 )) || fail 'Invalid SSH port'
case "$SSH_PORT" in 443|2222) fail 'SSH port conflicts with node ports' ;; esac
[[ "$CERT_DOMAIN" =~ ^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$ && "$CERT_DOMAIN" == *.* && "$CERT_DOMAIN" != *..* ]] || fail 'Invalid certificate domain'
case "$SITE_VERSION" in 0|1) ;; *) fail 'Unsupported HTTPS site version' ;; esac
[ "$#" -eq 6 ] || [ "$#" -eq 7 ] || fail 'Unexpected TLS action arguments'
case "$ACTION" in preflight|packages|install|verify|identity|site-install|site-verify) ;; *) fail 'Unknown action' ;; esac
[ "$(id -u)" = 0 ] || fail 'Root is required'
. /etc/os-release
[ "$ID:$VERSION_ID" = ubuntu:24.04 ] || fail 'Ubuntu 24.04 is required'
[ -d "$STAGE" ] && [ ! -L "$STAGE" ] && [ "$(stat -c '%u:%a' "$STAGE")" = 0:700 ] \
    || fail 'Unsafe staging directory'

safe_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u:%a:%h' "$1")" = 0:600:1 ] \
        || fail 'Unsafe managed file'
}
owned_node() {
    [ -d "$NODE" ] && [ ! -L "$NODE" ] && [ -f "$NODE/.spn-deployment" ] \
        && [ ! -L "$NODE/.spn-deployment" ] \
        && [ "$(stat -c '%u:%a:%h' "$NODE/.spn-deployment")" = 0:600:1 ] \
        && [ "$(cat "$NODE/.spn-deployment")" = "$DEPLOY_ID" ]
}
target_network() {
    local count source
    count=$(ip -o -4 addr show up scope global | awk -v wanted="$NODE_IP" '
        { split($4,address,"/"); if (address[1] == wanted) found++ } END { print found+0 }')
    [ "$count" = 1 ] || fail 'Target IPv4 is not assigned once to an UP interface'
    # A purchased extra IPv4 may not yet be the preferred source. The local
    # installer invokes preferred_source.py with backup/verification/rollback
    # before installing the node; mere mismatch is not a preflight failure.
}
fresh_listeners_safe() {
    # Enabling a deny-incoming policy must not strand an unrelated native service.
    # A fresh VPS may expose SSH; local resolver and other loopback sockets stay local.
    ss -H -lnt | awk -v ssh="$SSH_PORT" '
        {
            address=$4; port=address
            sub(/^.*:/, "", port)
            sub(/:[^:]*$/, "", address)
            sub(/%.*/, "", address)
            if (port == ssh) next
            if (address ~ /^127\./ || address == "[::1]" || address == "::1") next
            if (address ~ /^\[::ffff:127\./ || address ~ /^::ffff:127\./) next
            unsafe=1
        }
        END { exit unsafe }
    '
}
install_owned() {
    local source=$1 destination=$2
    safe_file "$source"
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        safe_file "$destination"
        cmp -s "$source" "$destination" || fail 'Installed file differs; refusing to overwrite it'
    else
        install -m 0600 "$source" "$destination"
    fi
}
check_containers() {
    local container
    if command -v docker >/dev/null; then
        docker info >/dev/null 2>&1 || fail 'Docker is installed but unavailable; run systemctl start docker, then repeat the same installer command'
        for container in $(docker ps -aq); do
            [ "$(docker inspect -f '{{.Name}}|{{index .Config.Labels "spn.deploy.id"}}' "$container")" = "/remnanode|$DEPLOY_ID" ] \
                || fail 'An unrelated Docker container exists; use a clean VPS'
            [ "$(docker inspect -f '{{.Config.Image}}|{{.HostConfig.NetworkMode}}' "$container")" = "$IMAGE|host" ] \
                || fail 'Owned container image/network settings changed'
        done
    fi
}
panel_rule_present() {
    # Parse exact fields. Comments, ALLOW IN and a /32 host are normal UFW output.
    # A broad/unexpected inbound rule for port 2222 must not be accepted.
    awk -v source="$PANEL_IP" '
        function covers_api(target, pieces, count, i, ends, bounds) {
            if (target ~ /\/udp$/) return 0
            sub(/\/tcp$/, "", target)
            if (target == "Anywhere") return 1
            count = split(target, pieces, ",")
            for (i=1; i<=count; i++) {
                if (pieces[i] == "2222") return 1
                bounds = split(pieces[i], ends, ":")
                if (bounds == 2 && ends[1] ~ /^[0-9]+$/ && ends[2] ~ /^[0-9]+$/ &&
                    ends[1]+0 <= 2222 && ends[2]+0 >= 2222) return 1
            }
            return 0
        }
        { sub(/[[:space:]]*#.*/, "") }
        covers_api($1) {
            action = 0
            for (i=2; i<=NF; i++) if ($i == "ALLOW") { action=i; break }
            if (!action || $(action+1) == "OUT") next
            field = ($(action+1) == "IN" ? action+2 : action+1)
            address = $field; sub(/\/32$/, "", address)
            if (NF != field || address != source) unsafe = 1
            else if ($1 == "2222/tcp" && action == 2) found = 1
        }
        END { exit !(found && !unsafe) }
    '
}
firewall_defaults_safe() {
    awk '
        index($0, "Default: deny (incoming), allow (outgoing),") == 1 { found=1 }
        END { exit !found }
    '
}
public_rule_present() {
    local port=$1
    awk -v port="$port/tcp" '
        { sub(/[[:space:]]*#.*/, "") }
        $1 == port && $2 == "ALLOW" {
            field = ($3 == "IN" ? 4 : 3)
            if (NF == field && $field == "Anywhere") found = 1
        }
        END { exit !found }
    '
}
ipv4_connect() {
    # An actual IPv4 socket is stronger evidence than the machine-wide IPv6
    # default: applications can override IPV6_V6ONLY for their own sockets.
    # Connect locally to the assigned public address; send no application data.
    python3 - "$NODE_IP" "$1" <<'PY'
import socket
import sys

try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.4)
        connection.connect((sys.argv[1], int(sys.argv[2])))
except (OSError, ValueError):
    raise SystemExit(1)
PY
}
listeners_public() {
    local port=$1 listeners
    listeners=$(ss -H -lnt "sport = :$port") || return 1
    # Go/Node wildcard listeners can be shown as *, [::] or 0.0.0.0.
    # Require BOTH a wildcard socket and demonstrated IPv4 connectivity;
    # loopback-only and IPv6-only listeners must never count as ready.
    printf '%s\n' "$listeners" | awk -v port="$port" '
        $4 == "0.0.0.0:"port || $4 == "*:"port || $4 == "[::]:"port { found = 1 }
        END { exit !found }
    ' || return 1
    ipv4_connect "$port"
}
wait_listeners() {
    local deadline=$((SECONDS+45))
    # A reconnecting panel can briefly report the pre-reinstall node state.
    # Only observe listener readiness here; never restart services or alter it.
    # Bound elapsed time, not attempts, since socket probes themselves take time.
    while :; do
        if listeners_public 443 && listeners_public 2222; then return 0; fi
        (( SECONDS < deadline )) || return 1
        sleep 1
    done
}
diagnose_listeners() {
    local port addresses connection
    for port in 443 2222; do
        # Never expose ss process/PID fields, environment or raw core logs.
        addresses=$(ss -H -lnt "sport = :$port" 2>/dev/null | awk '{print $4}' | sort -u | paste -sd, -) || addresses=unavailable
        [ -n "$addresses" ] || addresses=none
        if ipv4_connect "$port"; then connection=ok; else connection=failed; fi
        printf 'TCP%s: local IPv4 connection=%s; listener addresses=%s\n' "$port" "$connection" "$addresses" >&2
    done
}

install_site_packages() {
    [ "$SITE_VERSION" = 1 ] || return 0
    if command -v nginx >/dev/null && [ ! -e "$STAGE/tls-site-packages.pending" ]; then return 0; fi
    if [ ! -e "$STAGE/tls-site-packages.pending" ]; then
        # A package we install must not replace an administrator's custom unit.
        [ ! -e /etc/systemd/system/nginx.service ] && [ ! -L /etc/systemd/system/nginx.service ] \
            && [ ! -e /etc/systemd/system/nginx.service.d ] || fail 'An unmanaged nginx unit exists'
        touch "$STAGE/tls-site-packages.pending"
    fi
    safe_file "$STAGE/tls-site-packages.pending"
    safe_file "$STAGE/tls-site-policy.sh"
    # Debian packages otherwise start the distribution's public TCP80 server.
    # Prevent just nginx starts, then use our dedicated loopback-only instance.
    # Existing policy is honored; no foreign policy file is overwritten.
    local policy=/usr/sbin/policy-rc.d status candidate=/usr/sbin/.spn-nginx-policy-$DEPLOY_ID temporary
    if [ -e "$candidate" ] || [ -L "$candidate" ]; then
        [ -f "$candidate" ] && [ ! -L "$candidate" ] && [ "$(stat -c '%u:%a' "$candidate")" = 0:755 ] \
            && cmp -s "$STAGE/tls-site-policy.sh" "$candidate" || fail 'Owned nginx package policy was changed'
    fi
    if { [ -e "$policy" ] || [ -L "$policy" ]; } && { [ ! -e "$candidate" ] || [ ! "$policy" -ef "$candidate" ]; }; then
        if "$policy" nginx start; then status=0; else status=$?; fi
        [ "$status" = 101 ] || fail 'Existing service policy allows nginx to start; install nginx without starting its public default server, then retry'
        apt_install nginx
    else
        if [ ! -e "$candidate" ]; then
            temporary=$(mktemp /usr/sbin/.spn-nginx-policy.XXXXXXXX)
            cat "$STAGE/tls-site-policy.sh" > "$temporary"
            chmod 0755 "$temporary"
            if ! ln "$temporary" "$candidate"; then rm "$temporary"; fail 'Nginx package policy appeared concurrently'; fi
            rm "$temporary"
        fi
        # ln fails if another administrator creates policy-rc.d concurrently.
        if [ ! -e "$policy" ] && [ ! -L "$policy" ]; then
            ln "$candidate" "$policy" || fail 'Service policy appeared during nginx installation'
        fi
        SITE_PACKAGE_POLICY=$candidate
        SITE_PACKAGE_POLICY_PATH=$policy
        cleanup_site_package_policy() {
            if [ -f "$SITE_PACKAGE_POLICY_PATH" ] && [ ! -L "$SITE_PACKAGE_POLICY_PATH" ] && [ "$SITE_PACKAGE_POLICY_PATH" -ef "$SITE_PACKAGE_POLICY" ]; then
                rm "$SITE_PACKAGE_POLICY_PATH"
            fi
            if [ -f "$SITE_PACKAGE_POLICY" ] && [ ! -L "$SITE_PACKAGE_POLICY" ] \
                && cmp -s "$STAGE/tls-site-policy.sh" "$SITE_PACKAGE_POLICY"; then rm "$SITE_PACKAGE_POLICY"; fi
        }
        trap cleanup_site_package_policy EXIT
        apt_install nginx
        cleanup_site_package_policy
        trap - EXIT
    fi
    # Package-created enablement would start the default site after a reboot.
    # This branch is reached only when nginx was absent before our installation.
    [ ! -e /etc/systemd/system/nginx.service ] && [ ! -L /etc/systemd/system/nginx.service ] \
        && [ ! -e /etc/systemd/system/nginx.service.d ] || fail 'An unmanaged nginx unit appeared'
    systemctl disable nginx
    rm "$STAGE/tls-site-packages.pending"
}
safe_site_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u:%a:%h' "$1")" = 0:644:1 ] \
        || fail 'Unsafe managed HTTPS site file'
}
install_site() {
    [ "$SITE_VERSION" = 1 ] || return 0
    for file in tls-site-nginx.conf tls-site.service tls-site-index.html tls-site-policy.sh; do safe_file "$STAGE/$file"; done
    install_site_packages
    if [ -e "$SITE" ] || [ -L "$SITE" ]; then
        [ -d "$SITE" ] && [ ! -L "$SITE" ] && [ "$(stat -c '%u:%a' "$SITE")" = 0:755 ] \
            || fail 'Unsafe HTTPS site directory'
        safe_file "$SITE/.spn-deployment"
        [ "$(cat "$SITE/.spn-deployment")" = "$DEPLOY_ID" ] || fail 'HTTPS site ownership differs'
    else
        local claim
        claim=$(mktemp -d /var/lib/.spn-tls-site-claim.XXXXXXXX)
        install -m 0600 "$STAGE/.spn-deployment" "$claim/.spn-deployment"
        install -m 0644 "$STAGE/tls-site-index.html" "$claim/index.html"
        chmod 0755 "$claim"
        mv -Tn "$claim" "$SITE"
        if [ -d "$claim" ]; then
            rm "$claim/.spn-deployment" "$claim/index.html"; rmdir "$claim"
        fi
        [ -d "$SITE" ] && [ ! -L "$SITE" ] && [ "$(stat -c '%u:%a' "$SITE")" = 0:755 ] \
            || fail 'HTTPS site directory ownership differs'
        safe_file "$SITE/.spn-deployment"
        [ "$(cat "$SITE/.spn-deployment")" = "$DEPLOY_ID" ] || fail 'HTTPS site was claimed by another installation'
    fi
    safe_site_file "$SITE/index.html"
    cmp -s "$STAGE/tls-site-index.html" "$SITE/index.html" || fail 'Managed HTTPS page was changed'
    install_owned "$STAGE/tls-site-nginx.conf" "$NODE/tls-site-nginx.conf"
    if [ -e "$SITE_UNIT" ] || [ -L "$SITE_UNIT" ]; then
        safe_site_file "$SITE_UNIT"
        cmp -s "$STAGE/tls-site.service" "$SITE_UNIT" || fail 'Managed HTTPS service was changed'
    else
        install -m 0644 "$STAGE/tls-site.service" "$SITE_UNIT"
    fi
    # RuntimeDirectory is created before the unit's nginx -t ExecStartPre.
    systemctl daemon-reload
    systemctl enable --now "$SITE_SERVICE"
}
site_public_pages_ready() {
    local protocol
    for protocol in --http1.1 --http2; do
        curl --noproxy '*' "$protocol" --proto '=https' --tlsv1.3 --fail --silent \
            --connect-timeout 2 --max-time 5 --resolve "$CERT_DOMAIN:443:$NODE_IP" "https://$CERT_DOMAIN/" \
            | cmp -s - "$SITE/index.html" || return 1
    done
}
wait_site_public_pages() {
    # A successful panel PATCH precedes asynchronous delivery to the node.
    # Keep failed attempts quiet, bounded by both elapsed time and curl timeouts.
    local deadline=$((SECONDS+45))
    while :; do
        site_public_pages_ready && return 0
        (( SECONDS < deadline )) || return 1
        sleep 1
    done
}
verify_site() {
    [ "$SITE_VERSION" = 1 ] || return 0
    safe_file "$NODE/tls-site-nginx.conf"
    safe_site_file "$SITE_UNIT"; safe_site_file "$SITE/index.html"
    cmp -s "$STAGE/tls-site-nginx.conf" "$NODE/tls-site-nginx.conf" \
        && cmp -s "$STAGE/tls-site.service" "$SITE_UNIT" \
        && cmp -s "$STAGE/tls-site-index.html" "$SITE/index.html" || fail 'HTTPS site configuration differs'
    systemctl is-enabled --quiet "$SITE_SERVICE" && systemctl is-active --quiet "$SITE_SERVICE" \
        || fail 'HTTPS site service is not enabled and active'
    # Exercise both unencrypted fallback protocols and the actual public TLS
    # listener with certificate verification. A listening port alone is not proof.
    curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 5 \
        http://127.0.0.1:18080/ | cmp -s - "$SITE/index.html" || fail 'HTTP/1.1 fallback page is unavailable'
    curl --noproxy '*' --http2-prior-knowledge --fail --silent --show-error --connect-timeout 2 --max-time 5 \
        http://127.0.0.1:18081/ | cmp -s - "$SITE/index.html" || fail 'HTTP/2 fallback page is unavailable'
    wait_site_public_pages || fail 'HTTPS page or TLS fallback did not become ready within 45 seconds'
    # CERT_NONE deliberately disables client rejection. These handshakes must
    # fail at the server so arbitrary-IP scans do not receive our certificate.
    python3 - "$NODE_IP" "$DEPLOY_ID" <<'PY'
import socket
import ssl
import sys

for name in ('unrelated-' + sys.argv[2] + '.example.invalid', None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(['http/1.1'])
    with socket.create_connection((sys.argv[1], 443), timeout=5) as connection:
        try:
            with context.wrap_socket(connection, server_hostname=name):
                pass
        except ssl.SSLError:
            continue
    raise SystemExit('TLS listener accepted an unknown or absent SNI')
PY
}

# HTTP-01 is handled by Certbot's temporary standalone listener on TCP80.
# Certificate files remain private; DNS/API tokens never leave the installer.
certificate_fresh() {
    # OpenSSL 3.6.0 can return zero for an expiring certificate (#28928).
    # Require its explicit positive result as well as a successful pipeline.
    openssl x509 -in "/etc/letsencrypt/live/$CERT_DOMAIN/fullchain.pem" -noout -checkend 86400 \
        | grep -Fxq 'Certificate will not expire'
}
verify_certificate() {
    local cert=/etc/letsencrypt/live/$CERT_DOMAIN
    [ -f "$cert/fullchain.pem" ] && [ -f "$cert/privkey.pem" ] || fail 'TLS certificate is missing'
    openssl verify -CApath /etc/ssl/certs -untrusted "$cert/fullchain.pem" \
        -purpose sslserver -verify_hostname "$CERT_DOMAIN" "$cert/fullchain.pem" >/dev/null \
        || fail 'TLS certificate chain/domain is not trusted'
    certificate_fresh || fail 'TLS certificate expires within one day'
    openssl x509 -in "$cert/fullchain.pem" -noout -pubkey > "$STAGE/cert-public.tmp"
    openssl pkey -in "$cert/privkey.pem" -pubout > "$STAGE/key-public.tmp"
    cmp -s "$STAGE/cert-public.tmp" "$STAGE/key-public.tmp" || fail 'TLS private key differs'
}
install_certificate() {
    local renewal=/etc/letsencrypt/renewal/$CERT_DOMAIN.conf
    local claim=$NODE/tls-domain
    if [ -e "$STAGE/tls-certificate.bundle.json" ] || [ -L "$STAGE/tls-certificate.bundle.json" ]; then
        safe_file "$STAGE/tls-certificate.bundle.json"
        safe_file "$STAGE/tls_certificate_import.py"
        # A clean replacement receives only its isolated ACME account and TLS
        # lineage. Cloudflare credentials remain on the controller. The helper
        # writes standard standalone HTTP-01 renewal settings for after cutover.
        python3 "$STAGE/tls_certificate_import.py" --bundle "$STAGE/tls-certificate.bundle.json" \
            --deployment "$DEPLOY_ID" --domain "$CERT_DOMAIN"
    fi
    if [ -e "$claim" ] || [ -L "$claim" ]; then
        safe_file "$claim"
        [ "$(cat "$claim")" = "$CERT_DOMAIN" ] || fail 'Owned TLS certificate domain differs'
    else
        # Claim the domain before issuing, so interruption can safely resume.
        [ ! -e "$renewal" ] && [ ! -e "/etc/letsencrypt/live/$CERT_DOMAIN" ] \
            || fail 'An unrelated certificate already exists for this domain'
        printf '%s\n' "$CERT_DOMAIN" > "$claim"
    fi
    # Same-VPS IP changes retain the certificate. Never ask the CA to connect
    # to the still-old DNS address when a usable certificate already exists.
    if [ ! -f "$renewal" ] || [ ! -f "/etc/letsencrypt/live/$CERT_DOMAIN/fullchain.pem" ] \
        || ! certificate_fresh; then
        [ -z "$(ss -H -lnt 'sport = :80')" ] || fail 'TCP80 must be available for certificate issuance'
        certbot certonly --standalone --preferred-challenges http --non-interactive --agree-tos \
            --register-unsafely-without-email --keep-until-expiring --cert-name "$CERT_DOMAIN" -d "$CERT_DOMAIN"
    fi
    verify_certificate
    local hooks=/etc/letsencrypt/renewal-hooks/deploy hook
    install -d -m 0755 "$hooks"
    hook=$hooks/spn-$DEPLOY_ID
    cat > "$STAGE/cert-renew-hook" <<HOOK
#!/bin/sh
set -eu
[ "\${RENEWED_LINEAGE:-}" = "/etc/letsencrypt/live/$CERT_DOMAIN" ] || exit 0
[ "\$(cat $NODE/.spn-deployment)" = "$DEPLOY_ID" ] || exit 1
[ "\$(docker inspect -f '{{index .Config.Labels "spn.deploy.id"}}' remnanode)" = "$DEPLOY_ID" ] || exit 1
docker restart remnanode >/dev/null
HOOK
    if [ -e "$hook" ] || [ -L "$hook" ]; then
        [ -f "$hook" ] && [ ! -L "$hook" ] && [ "$(stat -c '%u:%a:%h' "$hook")" = 0:700:1 ] \
            && cmp -s "$STAGE/cert-renew-hook" "$hook" || fail 'Certificate renewal hook was changed'
    else
        install -m 0700 "$STAGE/cert-renew-hook" "$hook"
    fi
    systemctl enable --now certbot.timer
}

prepare_packages() {
    if [ -e "$STAGE/packages-ok" ] || [ -L "$STAGE/packages-ok" ]; then safe_file "$STAGE/packages-ok"; fi
    if [ ! -f "$STAGE/packages-ok" ] || ! command -v python3 >/dev/null || ! command -v certbot >/dev/null \
        || ! command -v curl >/dev/null || ! command -v docker >/dev/null || ! command -v ufw >/dev/null \
        || ! command -v openssl >/dev/null; then
        export DEBIAN_FRONTEND=noninteractive
        apt_update
        apt_install ca-certificates curl python3 docker.io docker-compose-v2 ufw certbot openssl
        systemctl enable --now docker
        docker compose version >/dev/null
        touch "$STAGE/packages-ok"
    fi
    # Nginx has package hooks too. Its existing start policy prevents the
    # distribution's public default site from occupying the certificate port.
    install_site_packages
}

case "$ACTION" in
packages)
    target_network
    safe_file "$STAGE/preflight-ok"
    [ "$(cat "$STAGE/preflight-ok")" = "$NODE_IP|$PANEL_IP|$SSH_PORT|$DEPLOY_ID" ] || fail 'Preflight parameters differ'
    if [ -e "$NODE" ] || [ -L "$NODE" ]; then owned_node || fail 'Existing installation is not owned by this deployment'; fi
    check_containers
    prepare_packages
    printf 'Managed TLS packages ready\n'
    ;;
site-install)
    [ "$SITE_VERSION" = 1 ] || fail 'Explicit HTTPS site version is required'
    target_network
    owned_node || fail 'Missing deployment ownership marker'
    install_site
    ;;
site-verify)
    [ "$SITE_VERSION" = 1 ] || fail 'Explicit HTTPS site version is required'
    target_network
    owned_node || fail 'Missing deployment ownership marker'
    verify_certificate
    verify_site
    ;;
identity)
    [ -f /etc/machine-id ] && [ ! -L /etc/machine-id ] || fail 'Machine identity missing'
    [ -f /etc/ssh/ssh_host_ed25519_key.pub ] || fail 'ED25519 host key missing'
    machine=$(tr -d '\n' < /etc/machine-id)
    fingerprint=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256 | awk '{print $2}')
    [[ "$machine" =~ ^[0-9a-f]{32}$ && "$fingerprint" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || fail 'Invalid machine identity'
    if owned_node; then managed=true; else managed=false; fi
    printf '{"machineId":"%s","sshFingerprint":"%s","ownedInstallation":%s}\n' "$machine" "$fingerprint" "$managed"
    ;;
preflight)
    target_network
    if [ -e "$NODE" ] || [ -L "$NODE" ]; then owned_node || fail 'Existing installation is not owned by this deployment'; fi
    for legacy in /opt/remnanode /opt/remnanode-single75; do
        [ ! -e "$legacy" ] && [ ! -L "$legacy" ] || fail 'An older node installation exists; use a clean VPS'
    done
    check_containers
    if ! owned_node; then
        fresh_listeners_safe || fail 'An unrelated non-loopback TCP service exists; use a clean VPS'
        for port in 80 443 2222; do
            [ -z "$(ss -H -lnt "sport = :$port")" ] || fail "TCP$port is occupied"
        done
        if command -v ufw >/dev/null && ufw status | grep -Fxq 'Status: active'; then
            fail 'An unrelated active UFW firewall exists; use a clean VPS'
        fi
        if command -v ufw >/dev/null && ufw show added | grep -Eq '^ufw[[:space:]]'; then
            fail 'Unmanaged stored UFW rules exist; refusing to enable them'
        fi
    fi
    if [ -e "$STAGE/preflight-ok" ] || [ -L "$STAGE/preflight-ok" ]; then
        safe_file "$STAGE/preflight-ok"
        [ "$(cat "$STAGE/preflight-ok")" = "$NODE_IP|$PANEL_IP|$SSH_PORT|$DEPLOY_ID" ] || fail 'Saved target parameters differ'
    else
        printf '%s\n' "$NODE_IP|$PANEL_IP|$SSH_PORT|$DEPLOY_ID" > "$STAGE/preflight-ok"
    fi
    printf 'VPS preflight OK\n'
    ;;
install)
    target_network
    safe_file "$STAGE/preflight-ok"
    [ "$(cat "$STAGE/preflight-ok")" = "$NODE_IP|$PANEL_IP|$SSH_PORT|$DEPLOY_ID" ] || fail 'Preflight parameters differ'
    for file in node.env docker-compose.yml .spn-deployment; do safe_file "$STAGE/$file"; done
    [ "$(cat "$STAGE/.spn-deployment")" = "$DEPLOY_ID" ] || fail 'Staged ownership differs'
    if [ -e "$NODE" ] || [ -L "$NODE" ]; then owned_node || fail 'Existing installation is not owned by this deployment'; fi
    check_containers
    prepare_packages
    if [ ! -e "$NODE" ]; then
        # Atomically claim this directory, so a crash never leaves an unowned path.
        claim=$(mktemp -d /opt/.spn-reality-claim.XXXXXXXX)
        install -m 0600 "$STAGE/.spn-deployment" "$claim/.spn-deployment"
        mv -Tn "$claim" "$NODE"
        if [ -d "$claim" ]; then rm "$claim/.spn-deployment"; rmdir "$claim"; fi
    fi
    owned_node || fail 'Ownership marker differs'
    install_owned "$STAGE/node.env" "$NODE/node.env"
    install_owned "$STAGE/docker-compose.yml" "$NODE/docker-compose.yml"
    if [ -e "$STAGE/node_guard.py" ] || [ -L "$STAGE/node_guard.py" ]; then
        for file in node_guard.py scanner_guard.py scanner_lists.json; do safe_file "$STAGE/$file"; done
        if [ -f "$STAGE/node_guard_upgrade.py" ]; then
            safe_file "$STAGE/node_guard_upgrade.py"
            python3 "$STAGE/node_guard_upgrade.py" --deployment "$DEPLOY_ID"
        fi
        python3 "$STAGE/node_guard.py" install --active-ip "$NODE_IP" --deployment "$DEPLOY_ID" \
            --panel-ip "$PANEL_IP" --controller-ip 147.45.164.28
    fi
    install_site
    if [ ! -f "$STAGE/firewall-ok" ]; then
        ufw status verbose > "$STAGE/firewall.before"
        ufw allow "$SSH_PORT/tcp" comment SPN-REALITY-SSH
        ufw allow 443/tcp comment SPN-TLS-CLIENTS
        ufw allow 80/tcp comment SPN-TLS-CERTIFICATE
        ufw allow from "$PANEL_IP" to any port 2222 proto tcp comment SPN-REALITY-PANEL
        ufw default deny incoming
        ufw default allow outgoing
        ufw --force enable
        touch "$STAGE/firewall-ok"
    fi
    install_certificate
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then docker pull "$IMAGE"; fi
    docker compose -f "$NODE/docker-compose.yml" up -d --pull never
    verify_certificate
    touch "$STAGE/install-ok"
    printf 'Managed TLS node installation ready\n'
    ;;
verify)
    target_network
    owned_node || fail 'Missing deployment ownership marker'
    safe_file "$NODE/node.env"; safe_file "$NODE/docker-compose.yml"
    cmp -s "$STAGE/node.env" "$NODE/node.env" && cmp -s "$STAGE/docker-compose.yml" "$NODE/docker-compose.yml" \
        || fail 'Managed configuration differs'
    check_containers
    verify_certificate
    systemctl is-enabled --quiet certbot.timer || fail 'Certificate renewal timer is not enabled'
    systemctl is-active --quiet certbot.timer || fail 'Certificate renewal timer is not active'
    ufw status | public_rule_present 80 || fail 'Certificate firewall rule is missing'
    [ "$(docker inspect -f '{{.State.Running}}' remnanode)" = true ] || fail 'Node container is stopped'
    [ "$(docker inspect -f '{{range .HostConfig.Ulimits}}{{if eq .Name "nofile"}}{{.Soft}}:{{.Hard}}{{end}}{{end}}' remnanode)" = 65536:65536 ] \
        || fail 'Container nofile limit differs'
    ufw status | grep -Fxq 'Status: active' || fail 'Firewall is not active'
    ufw status verbose | firewall_defaults_safe || fail 'Firewall defaults must deny incoming and allow outgoing'
    ufw status | public_rule_present "$SSH_PORT" || fail 'SSH firewall rule is missing'
    ufw status | public_rule_present 443 || fail 'Client firewall rule is missing'
    ufw status | panel_rule_present || fail 'Exact restricted panel firewall rule is missing or broad port 2222 rule exists'
    if [ -e "$STAGE/node_guard.py" ] || [ -L "$STAGE/node_guard.py" ]; then
        for file in node_guard.py scanner_guard.py scanner_lists.json; do safe_file "$STAGE/$file"; done
        python3 "$STAGE/node_guard.py" verify --active-ip "$NODE_IP" --deployment "$DEPLOY_ID" \
            --panel-ip "$PANEL_IP" --controller-ip 147.45.164.28
    fi
    if ! wait_listeners; then
        diagnose_listeners
        fail 'TCP443 or API2222 did not become IPv4-ready within 45 seconds; retry the same installer command'
    fi
    verify_site
    printf 'Node listeners, Docker limits and restricted firewall verified; client traffic still needs an app test\n'
    ;;
esac
