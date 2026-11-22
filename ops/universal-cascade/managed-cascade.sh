#!/bin/bash
# Run by deploy.py over authenticated SSH. One owned cascade role per guest.
set -euo pipefail
umask 077
export LC_ALL=C

fail() { printf 'Stopped: %s\n' "$*" >&2; exit 1; }
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
valid_port() { [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && (( $1 <= 65535 )); }
[ "$#" = 11 ] || [ "$#" = 13 ] || fail 'Expected action, role, public addresses, panel, SSH, deployment, interface, tunnel addresses and UDP port, optionally entry and RU UDP ports'
ACTION=$1; ROLE=$2; LOCAL_PUBLIC=$3; PEER_PUBLIC=$4; PANEL_IP=$5; SSH_PORT=$6
DEPLOY_ID=$7; INTERFACE=$8; LOCAL_WG=$9; PEER_WG=${10}; WG_PORT=${11}
ENTRY_PORT=${12:-443}; RU_WG_PORT=${13:-$WG_PORT}
case "$ACTION" in preflight|install|verify|identity|inventory) ;; *) fail 'Unknown action' ;; esac
case "$ROLE" in ru|eu) ;; *) fail 'Invalid cascade role' ;; esac
[[ "$DEPLOY_ID" =~ ^[0-9a-f]{32}$ ]] || fail 'Invalid deployment ID'
[[ "$INTERFACE" =~ ^[a-z][a-z0-9]{0,14}$ ]] || fail 'Invalid WireGuard interface'
case "$INTERFACE" in lo|eth*|ens*|enp*|docker*|br*|tun*|tap*) fail 'Unsafe WireGuard interface name' ;; esac
for address in "$LOCAL_PUBLIC" "$PEER_PUBLIC" "$PANEL_IP" "$LOCAL_WG" "$PEER_WG"; do
    valid_ipv4 "$address" || fail 'Invalid IPv4'
done
valid_port "$SSH_PORT" && valid_port "$WG_PORT" && valid_port "$ENTRY_PORT" && valid_port "$RU_WG_PORT" || fail 'Invalid port'
case "$SSH_PORT" in 443|2222|2443) fail 'SSH port conflicts with cascade ports' ;; esac
[ "$ENTRY_PORT" != 2222 ] && [ "$ENTRY_PORT" != 2443 ] || fail 'Entry port conflicts with management'
if [ "$ROLE" = ru ]; then
    [ "$ENTRY_PORT" != "$SSH_PORT" ] || fail 'Entry port conflicts with RU SSH'
fi
[ "$LOCAL_PUBLIC" != "$PEER_PUBLIC" ] && [ "$LOCAL_WG" != "$PEER_WG" ] || fail 'Cascade peers must differ'
[ "$PANEL_IP" != "$LOCAL_PUBLIC" ] && [ "$PANEL_IP" != "$PEER_PUBLIC" ] || fail 'Panel must be separate from cascade servers'
IMAGE=remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e
STAGE=/root/spn-cascade-managed-$DEPLOY_ID
NODE=/opt/spn-cascade
OWNER="$DEPLOY_ID|$ROLE"
PARAMS="$ROLE|$LOCAL_PUBLIC|$PEER_PUBLIC|$PANEL_IP|$SSH_PORT|$DEPLOY_ID|$INTERFACE|$LOCAL_WG|$PEER_WG|$WG_PORT"
if [ "$ENTRY_PORT:$RU_WG_PORT" != "443:$WG_PORT" ]; then PARAMS="$PARAMS|$ENTRY_PORT|$RU_WG_PORT"; fi
if [ "$ROLE" = ru ]; then LOCAL_WG_PORT=$RU_WG_PORT; PEER_WG_PORT=$WG_PORT
else LOCAL_WG_PORT=$WG_PORT; PEER_WG_PORT=$RU_WG_PORT; fi
WG_FILE=/etc/wireguard/$INTERFACE.conf
PROXY_UNIT=/etc/systemd/system/spn-cascade-haproxy.service
DOCKER_UNIT=/etc/systemd/system/docker.service.d/spn-cascade.conf
command -v python3 >/dev/null || fail 'Ubuntu python3 is required'
python3 -c 'import ipaddress,sys
public=[ipaddress.IPv4Address(x) for x in sys.argv[1:4]]
private=[ipaddress.IPv4Address(x) for x in sys.argv[4:]]
ranges=[ipaddress.IPv4Network(x) for x in ("10.0.0.0/8","172.16.0.0/12","192.168.0.0/16")]
ok=all(x.is_global and not x.is_multicast for x in public) and all(any(x in n for n in ranges) for x in private)
raise SystemExit(0 if ok else 1)' "$LOCAL_PUBLIC" "$PEER_PUBLIC" "$PANEL_IP" "$LOCAL_WG" "$PEER_WG" \
    || fail 'Public addresses must be globally routable; tunnel addresses must be RFC1918'
[ "$(id -u)" = 0 ] || fail 'Root is required'
. /etc/os-release
[ "$ID:$VERSION_ID" = ubuntu:24.04 ] || fail 'Ubuntu 24.04 is required'

safe_file() {
    [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u:%a:%h' "$1")" = 0:600:1 ] \
        || fail 'Unsafe managed file'
}
safe_dir() {
    [ -d "$1" ] && [ ! -L "$1" ] && [ "$(stat -c '%u:%a' "$1")" = 0:700 ] \
        || fail 'Unsafe managed directory'
}
safe_dir "$STAGE"
if [ "$ROLE" = ru ]; then
    safe_file "$STAGE/shared_ru.py"
    exec python3 "$STAGE/shared_ru.py" "$@"
fi
[ "$ACTION" != inventory ] || fail 'Inventory is only available on RU'
safe_file "$STAGE/host_checks.py"
safe_file "$STAGE/host-policy.json"
monitoring=$(python3 "$STAGE/host_checks.py" monitoring "$STAGE/host-policy.json") || fail 'Cannot verify or preserve the existing monitoring listener'
case "$monitoring" in '[]') MONITOR_PORT='' ;; '[10050]') MONITOR_PORT=10050 ;; *) fail 'Invalid monitoring inventory' ;; esac
owned_node() {
    [ -d "$NODE" ] && [ ! -L "$NODE" ] && [ "$(stat -c '%u:%a' "$NODE")" = 0:700 ] \
        && [ -f "$NODE/.spn-deployment" ] && [ ! -L "$NODE/.spn-deployment" ] \
        && [ "$(stat -c '%u:%a:%h' "$NODE/.spn-deployment")" = 0:600:1 ] \
        && [ "$(cat "$NODE/.spn-deployment")" = "$OWNER" ]
}
owned_parameters() {
    safe_file "$NODE/.spn-parameters"
    [ "$(cat "$NODE/.spn-parameters")" = "$PARAMS" ] || fail 'Owned target parameters differ'
}
install_owned() {
    local source=$1 destination=$2
    safe_file "$source"
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        safe_file "$destination"
        cmp -s "$source" "$destination" || fail 'Installed configuration differs; refusing to overwrite it'
    else
        install -m 0600 "$source" "$destination"
    fi
}
apt_install() { apt-get -o DPkg::Lock::Timeout=600 install -y "$@"; }
apt_update() {
    local attempt status output
    # Let dpkg own its locks. Lists locks need their own bounded retry.
    for ((attempt=0; attempt<=120; attempt++)); do
        if output=$(apt-get -o DPkg::Lock::Timeout=600 update 2>&1); then
            printf '%s\n' "$output"; return 0
        else status=$?; fi
        if [[ "$output" != *"Could not get lock"* && "$output" != *"Unable to acquire"* ]]; then
            printf '%s\n' "$output" >&2; return "$status"
        fi
        if [ "$attempt" = 120 ]; then printf '%s\n' "$output" >&2; return "$status"; fi
        if [ "$attempt" = 0 ]; then printf 'Waiting for Ubuntu package locks (up to 10 minutes)…\n'; fi
        sleep 5
    done
}
target_network() {
    local count
    count=$(ip -o -4 addr show up scope global | awk -v wanted="$LOCAL_PUBLIC" '
        { split($4,a,"/"); if (a[1] == wanted) n++ } END { print n+0 }')
    [ "$count" = 1 ] || fail 'Target IPv4 is not assigned once to an UP interface'
}
preferred_source_safe() {
    # The local orchestrator may apply its separately owned preferred-source
    # helper between preflight and install. This script never rewrites routes.
    ip -4 route get "$PEER_PUBLIC" | awk -v wanted="$LOCAL_PUBLIC" '
        { for(i=1;i<NF;i++) if($i=="src" && $(i+1)==wanted) ok=1 } END { exit !ok }' \
        || fail 'Peer route does not use the selected public source IPv4'
}
routes_safe() {
    ip -j -4 route show table all | python3 -c 'import ipaddress,json,sys
local,peer=map(ipaddress.IPv4Address,sys.argv[1:3]); interface=sys.argv[3]
for route in json.load(sys.stdin):
    dst=route.get("dst", "default")
    if dst in ("default", "0.0.0.0/0"): continue
    net=ipaddress.IPv4Network(dst, strict=False)
    if local not in net and peer not in net: continue
    if route.get("dev")==interface and str(net) in (str(local)+"/32",str(peer)+"/32"): continue
    raise SystemExit(1)' "$LOCAL_WG" "$PEER_WG" "$INTERFACE" \
        || fail 'Tunnel addresses overlap an existing non-owned route or subnet'
}
fresh_listeners_safe() {
    # Inspect TCP and UDP: SSH and loopback services are the only exceptions.
    ss -H -lnut | awk -v ssh="$SSH_PORT" -v monitor="${MONITOR_PORT:-}" '
        {
            address=$5; port=address; sub(/^.*:/,"",port); sub(/:[^:]*$/,"",address); sub(/%.*/,"",address)
            if ($1=="tcp" && port==ssh) next
            if ($1=="tcp" && monitor!="" && port==monitor) next
            if (address ~ /^127\./ || address=="[::1]" || address=="::1") next
            if (address ~ /^\[::ffff:127\./ || address ~ /^::ffff:127\./) next
            # systemd-networkd DHCP sockets on a fresh cloud guest are not services.
            if ($1=="udp" && (port=="68" || port=="546")) next
            unsafe=1
        } END { exit unsafe }'
}
owned_listeners_safe() {
    ss -H -lnut | awk -v ssh="$SSH_PORT" -v monitor="${MONITOR_PORT:-}" -v udp="$WG_PORT" -v role="$ROLE" -v public="$LOCAL_PUBLIC" -v localip="$LOCAL_WG" '
        {
            address=$5; port=address; sub(/^.*:/,"",port); sub(/:[^:]*$/,"",address); sub(/%.*/,"",address)
            if ($1=="tcp" && port==ssh) next
            if ($1=="tcp" && monitor!="" && port==monitor) next
            if (address ~ /^127\./ || address=="[::1]" || address=="::1") next
            if (address ~ /^\[::ffff:127\./ || address ~ /^::ffff:127\./) next
            if ($1=="udp" && (port==udp || port=="68" || port=="546")) next
            if ($1=="tcp" && role=="ru" && port=="443" && address==public) next
            if ($1=="tcp" && role=="eu" && port=="2443" && address==localip) next
            if ($1=="tcp" && role=="eu" && port=="2222") next
            unsafe=1
        } END {exit unsafe}'
}
check_containers() {
    local container
    if command -v docker >/dev/null; then
        if ! docker info >/dev/null 2>&1; then
            owned_node || fail 'Preinstalled Docker is unavailable; an unowned data directory may contain containers'
            if owned_node && [ -f "$NODE/packages-ok" ]; then fail 'Owned Docker daemon is unavailable'; fi
            return 0
        fi
        for container in $(docker ps -aq); do
            [ "$ROLE" = eu ] && owned_node || fail 'A Docker container exists on an unowned or RU guest'
            [ "$(docker inspect -f '{{.Name}}|{{index .Config.Labels "spn.deploy.id"}}|{{index .Config.Labels "spn.deploy.role"}}' "$container")" = "/remnanode|$DEPLOY_ID|eu" ] \
                || fail 'An unrelated Docker container exists; use a clean VPS'
            [ "$(docker inspect -f '{{.Config.Image}}|{{.HostConfig.NetworkMode}}' "$container")" = "$IMAGE|host" ] \
                || fail 'Owned container image or host network mode changed'
        done
        if ! owned_node; then fail 'An unowned Docker daemon is active; use a clean VPS'; fi
    fi
}
check_wireguard_ownership() {
    local file interfaces claimed
    if [ -e /etc/wireguard ] || [ -L /etc/wireguard ]; then
        safe_dir /etc/wireguard
        for file in /etc/wireguard/* /etc/wireguard/.[!.]* /etc/wireguard/..?*; do
            [ -e "$file" ] || [ -L "$file" ] || continue
            owned_node && [ "$file" = "$WG_FILE" ] || fail 'Non-owned WireGuard configuration exists'
            safe_file "$file"
            safe_file "$NODE/wg.conf"
            cmp -s "$file" "$NODE/wg.conf" || fail 'WireGuard configuration differs'
        done
    fi
    interfaces=$(python3 "$STAGE/host_checks.py" wireguard) || fail 'Cannot inspect WireGuard interfaces'
    if owned_node; then claimed=1; else claimed=0; fi
    printf '%s\n' "$interfaces" | python3 -c 'import json,sys
try:
 names=json.load(sys.stdin)
 if not isinstance(names,list) or any(not isinstance(x,str) for x in names): raise ValueError()
 if any(sys.argv[1]!="1" or x!=sys.argv[2] for x in names): raise ValueError()
except (ValueError,TypeError): raise SystemExit(1)' "$claimed" "$INTERFACE" \
        || fail 'Non-owned or invalid WireGuard interface exists'
    if ip link show dev "$INTERFACE" >/dev/null 2>&1 && ! owned_node; then fail 'Requested tunnel interface already exists'; fi
}
fresh_firewall_safe() {
    if command -v ufw >/dev/null; then
        ! ufw status | grep -Fxq 'Status: active' || fail 'An unowned active UFW firewall exists'
        ! ufw show added | grep -Eq '^ufw[[:space:]]' || fail 'Unowned stored UFW rules exist'
        [ -z "$(dpkg --verify ufw)" ] || fail 'Installed UFW package configuration was modified'
    fi
    if command -v nft >/dev/null; then
        [ -z "$(nft list ruleset)" ] || fail 'An unowned nftables ruleset exists'
    fi
    local tool
    for tool in iptables-save ip6tables-save; do
        if command -v "$tool" >/dev/null; then
            "$tool" | awk '/^-/ {bad=1} /^:/ && $2!="ACCEPT" {bad=1} END {exit bad}' \
                || fail 'An unowned netfilter ruleset exists'
        fi
    done
}
unowned_configs_absent() {
    local path
    for path in /etc/docker/daemon.json /etc/haproxy "$PROXY_UNIT" /var/lib/docker /var/lib/containerd \
        /etc/systemd/system/docker.service.d /run/systemd/system/docker.service.d; do
        [ ! -e "$path" ] && [ ! -L "$path" ] || fail 'Non-owned proxy or container configuration/state exists'
    done
}
firewall_defaults_safe() {
    awk 'index($0,"Default: deny (incoming), allow (outgoing),")==1 {ok=1} END {exit !ok}'
}
firewall_rules_safe() {
    # Parse UFW stored commands semantically. Reject every rule outside this
    # exact allowlist, including broad ranges, interface omission and OUT rules.
    python3 -c 'import shlex,sys
role,ssh,udp,public,panel,interface,local,peer,monitor=sys.argv[1:]
expected={("tcp",ssh,"any","any",""),("udp",udp,public,"any","")}
if monitor: expected.add(("tcp",monitor,"any","any",""))
if role=="ru": expected.add(("tcp","443","any","any",""))
else: expected.update({("tcp","2222",panel,"any",""),("tcp","2443",peer,local,interface)})
seen=[]
try:
 for line in sys.stdin:
    if not line.startswith("ufw "): continue
    t=shlex.split(line)[1:]
    if "comment" in t: t=t[:t.index("comment")]
    if not t or t.pop(0)!="allow": raise ValueError()
    values={"proto":None,"port":None,"from":"any","to":"any","on":""}
    if t and t[0]=="in": t.pop(0)
    while t:
        token=t.pop(0)
        if token in values:
            if not t: raise ValueError()
            values[token]=t.pop(0)
        elif "/" in token and values["port"] is None:
            values["port"],values["proto"]=token.split("/")
        else: raise ValueError()
    for key in ("from","to"):
        if values[key].endswith("/32"): values[key]=values[key][:-3]
        if values[key]=="0.0.0.0/0": values[key]="any"
    seen.append(tuple(values[k] for k in ("proto","port","from","to","on")))
 if set(seen)!=expected or len(seen)!=len(expected): raise ValueError()
except (ValueError,IndexError): raise SystemExit(1)' \
        "$ROLE" "$SSH_PORT" "$LOCAL_WG_PORT" "$PEER_PUBLIC" "$PANEL_IP" "$INTERFACE" "$LOCAL_WG" "$PEER_WG" "${MONITOR_PORT:-}"
}
firewall_verified() {
    ufw status | grep -Fxq 'Status: active' || fail 'Firewall is not active'
    ufw status verbose | firewall_defaults_safe || fail 'Firewall defaults must deny incoming and allow outgoing'
    ufw show added | firewall_rules_safe || fail 'Firewall rules differ from the exact role allowlist'
    safe_file "$NODE/firewall.sha256"
    sha256sum --check --status "$NODE/firewall.sha256" || fail 'Managed firewall configuration changed'
}
validate_wg_config() {
    # Only wg-quick automatic peer-/32 routing is permitted: no DNS, hooks,
    # Table overrides, default route, extra peers or forwarding/NAT commands.
    python3 -c 'import base64,configparser,sys
try:
 p=configparser.ConfigParser(interpolation=None,strict=True); p.optionxform=str; p.read_file(sys.stdin)
 if p.sections()!=["Interface","Peer"] or p.defaults(): raise ValueError()
 i=dict(p["Interface"]); r=dict(p["Peer"])
 if set(i)!={"Address","PrivateKey","ListenPort","MTU"}: raise ValueError()
 if set(r)!={"PublicKey","PresharedKey","AllowedIPs","Endpoint","PersistentKeepalive"}: raise ValueError()
 if (i["Address"],i["ListenPort"],i["MTU"])!=(sys.argv[1]+"/32",sys.argv[4],"1380"): raise ValueError()
 if (r["AllowedIPs"],r["Endpoint"],r["PersistentKeepalive"])!=(sys.argv[2]+"/32",sys.argv[3]+":"+sys.argv[5],"25"): raise ValueError()
 for key in (i["PrivateKey"],r["PublicKey"],r["PresharedKey"]):
    raw=base64.b64decode(key,validate=True)
    if len(raw)!=32 or not any(raw) or base64.b64encode(raw).decode()!=key: raise ValueError()
except Exception: raise SystemExit(1)' "$LOCAL_WG" "$PEER_WG" "$PEER_PUBLIC" "$LOCAL_WG_PORT" "$PEER_WG_PORT" \
        || fail 'Unsafe or unexpected WireGuard configuration'
}
validate_proxy_config() {
    # This narrow grammar is deliberately fail-closed. In particular there is
    # one private backend, no TLS termination and no direct/public fallback.
    python3 -c 'import sys
expected="""global
user haproxy
group haproxy
maxconn 8192
ulimit-n 65536
tune.bufsize 16384
hard-stop-after 120s
defaults
mode tcp
timeout connect 3s
timeout queue 3s
timeout client 120s
timeout server 120s
timeout client-fin 5s
timeout server-fin 5s
timeout tunnel 120s
frontend spn_cascade_front
bind PUBLIC:443
maxconn 8192
tcp-request inspect-delay 5s
acl complete_client_hello req.ssl_hello_type 1
tcp-request content accept if complete_client_hello
tcp-request content reject if WAIT_END
default_backend spn_cascade_eu
backend spn_cascade_eu
server eu PRIVATE:2443 send-proxy-v2 maxconn 8192""".replace("PUBLIC",sys.argv[1]).replace("PRIVATE",sys.argv[2]).splitlines()
actual=[" ".join(line.split()) for line in sys.stdin if line.strip() and not line.lstrip().startswith("#")]
raise SystemExit(0 if actual==expected else 1)' "$LOCAL_PUBLIC" "$PEER_WG" \
        || fail 'Unexpected proxy configuration or non-private fallback'
}
validate_role_files() {
    safe_file "$STAGE/.spn-deployment"
    [ "$(cat "$STAGE/.spn-deployment")" = "$OWNER" ] || fail 'Staged ownership differs'
    safe_file "$STAGE/wg.conf"
    validate_wg_config < "$STAGE/wg.conf"
    if [ "$ROLE" = ru ]; then
        # These EU-only artifacts must never cross the RU trust boundary.
        for file in node.env docker-compose.yml; do
            [ ! -e "$STAGE/$file" ] && [ ! -L "$STAGE/$file" ] || fail 'EU-only configuration was staged on RU'
        done
        safe_file "$STAGE/haproxy.cfg"
        validate_proxy_config < "$STAGE/haproxy.cfg"
    else
        safe_file "$STAGE/node.env"; safe_file "$STAGE/docker-compose.yml"
        awk 'NR==1 && $0=="NODE_PORT=2222" {next}
             NR==2 && /^SECRET_KEY=[A-Za-z0-9_\/+==-]+$/ && length($0)>=31 {next}
             {bad=1} END {exit (bad || NR!=2)}' "$STAGE/node.env" || fail 'Unexpected EU node environment'
    fi
}
validate_compose() {
    # Never print rendered compose: it includes the node secret from node.env.
    docker compose -f "$NODE/docker-compose.yml" config --format json | python3 -c 'import json,sys
try:
 c=json.load(sys.stdin); services=c["services"]
 if set(services)!={"remnanode"}: raise ValueError()
 s=services["remnanode"]
 if (s.get("image"),s.get("network_mode"),s.get("container_name"))!=(sys.argv[1],"host","remnanode"): raise ValueError()
 if s.get("labels",{}).get("spn.deploy.id")!=sys.argv[2] or s.get("labels",{}).get("spn.deploy.role")!="eu": raise ValueError()
 if s.get("privileged") or any(s.get(k) for k in ("ports","volumes","cap_add","devices","command","entrypoint")): raise ValueError()
 if s.get("ulimits",{}).get("nofile")!={"soft":65536,"hard":65536}: raise ValueError()
 if set(s.get("environment",{}))!={"NODE_PORT","SECRET_KEY"} or str(s["environment"]["NODE_PORT"])!="2222": raise ValueError()
except Exception: raise SystemExit(1)' "$IMAGE" "$DEPLOY_ID" || fail 'Unexpected EU container configuration'
}
assert_owned_configs() {
    owned_node || fail 'Missing or different deployment ownership'
    owned_parameters
    safe_file "$NODE/wg.conf"; safe_file "$WG_FILE"
    cmp -s "$NODE/wg.conf" "$WG_FILE" || fail 'Managed WireGuard configuration changed'
    validate_wg_config < "$NODE/wg.conf"
    if [ "$ROLE" = eu ]; then
        safe_file "$NODE/node.env"; safe_file "$NODE/docker-compose.yml"
        safe_file "$NODE/docker-daemon.json"; safe_file /etc/docker/daemon.json
        cmp -s "$NODE/docker-daemon.json" /etc/docker/daemon.json || fail 'Owned Docker daemon settings changed'
        safe_file "$NODE/docker.service"; safe_file "$DOCKER_UNIT"
        cmp -s "$NODE/docker.service" "$DOCKER_UNIT" && cmp -s <(render_docker_unit) "$DOCKER_UNIT" || fail 'Owned Docker dependency changed'
    else
        safe_file "$NODE/haproxy.cfg"; safe_file "$PROXY_UNIT"
        safe_file "$NODE/haproxy.service"
        validate_proxy_config < "$NODE/haproxy.cfg"
        cmp -s "$NODE/haproxy.service" "$PROXY_UNIT" && cmp -s <(render_proxy_unit) "$PROXY_UNIT" || fail 'Owned proxy service changed'
    fi
}
write_docker_config() {
    local temp
    temp=$(mktemp "$STAGE/.docker.XXXXXXXX")
    # Host networking needs neither Docker bridge, NAT nor IP-forward changes.
    printf '%s\n' '{"bridge":"none","iptables":false,"ip6tables":false,"ip-forward":false,"ip-masq":false}' > "$temp"
    install_owned "$temp" "$NODE/docker-daemon.json"
    if [ ! -e /etc/docker ]; then install -d -m 0755 /etc/docker; fi
    [ -d /etc/docker ] && [ ! -L /etc/docker ] && [ "$(stat -c '%u' /etc/docker)" = 0 ] || fail 'Unsafe Docker configuration directory'
    install_owned "$temp" /etc/docker/daemon.json
    rm "$temp"
}
render_proxy_unit() {
    printf '%s\n' '[Unit]' 'Description=SPN cascade owned TCP passthrough' 'Wants=network-online.target' \
        "Requires=wg-quick@$INTERFACE.service" "After=network-online.target wg-quick@$INTERFACE.service" \
        '[Service]' 'Type=notify' 'ExecStart=/usr/sbin/haproxy -Ws -f /opt/spn-cascade/haproxy.cfg' \
        'Restart=on-failure' 'RestartSec=2' 'LimitNOFILE=131072' 'NoNewPrivileges=true' 'ProtectHome=true' \
        'PrivateTmp=true' '[Install]' 'WantedBy=multi-user.target'
}
render_docker_unit() {
    printf '%s\n' '[Unit]' "Requires=wg-quick@$INTERFACE.service" "After=wg-quick@$INTERFACE.service"
}
write_docker_unit() {
    local temp
    temp=$(mktemp "$STAGE/.docker-unit.XXXXXXXX")
    render_docker_unit > "$temp"
    if [ ! -e /etc/systemd/system/docker.service.d ]; then install -d -m 0755 /etc/systemd/system/docker.service.d; fi
    [ -d /etc/systemd/system/docker.service.d ] && [ ! -L /etc/systemd/system/docker.service.d ] \
        && [ "$(stat -c '%u' /etc/systemd/system/docker.service.d)" = 0 ] || fail 'Unsafe Docker unit directory'
    install_owned "$temp" "$NODE/docker.service"
    install_owned "$temp" "$DOCKER_UNIT"
    rm "$temp"
}
write_proxy_unit() {
    local temp
    temp=$(mktemp "$STAGE/.unit.XXXXXXXX")
    render_proxy_unit > "$temp"
    install_owned "$temp" "$NODE/haproxy.service"
    install_owned "$temp" "$PROXY_UNIT"
    rm "$temp"
}
install_packages() {
    local service
    if [ -e "$NODE/packages-ok" ] || [ -L "$NODE/packages-ok" ]; then
        safe_file "$NODE/packages-ok"
        for service in wg ufw; do command -v "$service" >/dev/null || fail 'Owned package missing'; done
        if [ "$ROLE" = ru ]; then command -v haproxy >/dev/null || fail 'Owned HAProxy missing'
        else command -v docker >/dev/null && docker compose version >/dev/null || fail 'Owned Docker missing'; fi
        return
    fi
    # Only services installed/claimed by this deployment can be masked here.
    # Mask before APT so postinst cannot start Docker with its NAT defaults.
    local -a masked=()
    if [ "$ROLE" = eu ]; then masked=(docker.service docker.socket containerd.service)
    else masked=(haproxy.service); fi
    local mask_parameters
    mask_parameters=$(printf '%s\n' "${masked[@]}")
    if [ -e "$NODE/package-masks" ] || [ -L "$NODE/package-masks" ]; then
        safe_file "$NODE/package-masks"
        [ "$(cat "$NODE/package-masks")" = "$mask_parameters" ] || fail 'Owned package mask parameters differ'
        for service in "${masked[@]}"; do
            if [ -e "/run/systemd/system/$service" ] || [ -L "/run/systemd/system/$service" ]; then
                [ -L "/run/systemd/system/$service" ] && [ "$(readlink "/run/systemd/system/$service")" = /dev/null ] || fail 'Existing runtime service override'
            fi
        done
    else
        for service in "${masked[@]}"; do
            [ ! -e "/run/systemd/system/$service" ] && [ ! -L "/run/systemd/system/$service" ] || fail 'Existing runtime service override'
        done
        printf '%s\n' "$mask_parameters" > "$NODE/package-masks"
    fi
    if [ "$ROLE" = eu ]; then
        write_docker_config
    fi
    # On error leave owned startup inhibited, so package defaults never open a
    # service or enable forwarding. A repeat recognizes only these owned masks.
    for service in "${masked[@]}"; do systemctl mask --runtime "$service"; done
    export DEBIAN_FRONTEND=noninteractive
    apt_update
    if [ "$ROLE" = eu ]; then apt_install ca-certificates python3 wireguard-tools ufw docker.io docker-compose-v2
    else apt_install ca-certificates python3 wireguard-tools ufw haproxy; fi
    for service in "${masked[@]}"; do systemctl unmask --runtime "$service"; done
    if [ "$ROLE" = eu ]; then
        # Compose config/version are client-only. Start the daemon only after
        # its WG boot dependency and the exact firewall have been installed.
        docker compose version >/dev/null
    else
        # This is the package service just installed above, never an unrelated
        # proxy. Only the dedicated owned unit is permitted to start HAProxy.
        systemctl disable haproxy.service
    fi
    touch "$NODE/packages-ok"
    rm "$NODE/package-masks"
}
install_firewall() {
    if [ -e "$NODE/firewall.sha256" ] || [ -L "$NODE/firewall.sha256" ]; then firewall_verified; return; fi
    # A failed first run can have a subset of our exact idempotent rules. UFW
    # duplicate suppression makes repeating these commands safe without resets.
    ufw allow "$SSH_PORT/tcp" comment SPN-CASCADE-SSH
    if [ -n "${MONITOR_PORT:-}" ]; then ufw allow "$MONITOR_PORT/tcp" comment SPN-CASCADE-EXISTING-MONITORING; fi
    ufw allow from "$PEER_PUBLIC" to any port "$LOCAL_WG_PORT" proto udp comment SPN-CASCADE-WG
    if [ "$ROLE" = ru ]; then ufw allow 443/tcp comment SPN-CASCADE-CLIENTS
    else
        ufw allow from "$PANEL_IP" to any port 2222 proto tcp comment SPN-CASCADE-PANEL
        ufw allow in on "$INTERFACE" from "$PEER_WG" to "$LOCAL_WG" port 2443 proto tcp comment SPN-CASCADE-CORE
    fi
    ufw show added | firewall_rules_safe || fail 'Unexpected firewall rules; refusing to enable them'
    ufw default deny incoming
    ufw default allow outgoing
    ufw --force enable
    # Track complete UFW sources as well as semantic allow rules, so changing
    # before/after rules cannot silently create a public bypass on repeat runs.
    local file
    for file in /etc/ufw/{ufw.conf,before.rules,after.rules,user.rules,before6.rules,after6.rules,user6.rules} /etc/default/ufw; do
        [ -f "$file" ] && [ ! -L "$file" ] && [ "$(stat -c '%u' "$file")" = 0 ] || fail 'Unsafe UFW configuration'
    done
    sha256sum /etc/ufw/{ufw.conf,before.rules,after.rules,user.rules,before6.rules,after6.rules,user6.rules} /etc/default/ufw > "$NODE/firewall.sha256"
    firewall_verified
}
listener_exact() {
    local port=$1 address=$2
    ss -H -lnt "sport = :$port" | awk -v wanted="$address:$port" '
        NF {n++; if($4!=wanted) bad=1} END {exit !(n==1 && !bad)}'
}
no_listener() { [ -z "$(ss -H -lnt "sport = :$1")" ]; }
probe_tcp() {
    # No TLS/VLESS credentials or application bytes are sent by readiness checks.
    python3 -c 'import socket,sys
try:
 with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
    s.settimeout(1); s.connect((sys.argv[1],int(sys.argv[2])))
except OSError: raise SystemExit(1)' "$1" "$2"
}
wireguard_runtime_verified() {
    local peer public now handshake psk actual_psk
    systemctl is-enabled --quiet "wg-quick@$INTERFACE.service" || fail 'WireGuard is not enabled'
    systemctl is-active --quiet "wg-quick@$INTERFACE.service" || fail 'WireGuard is not active'
    peer=$(awk '/^[[:space:]]*PublicKey[[:space:]]*=/ {sub(/^[^=]*=[[:space:]]*/,""); print}' "$NODE/wg.conf")
    public=$(awk '/^[[:space:]]*PrivateKey[[:space:]]*=/ {sub(/^[^=]*=[[:space:]]*/,""); print}' "$NODE/wg.conf" | wg pubkey)
    [ "$(wg show "$INTERFACE" public-key)" = "$public" ] || fail 'WireGuard identity changed'
    [ "$(wg show "$INTERFACE" peers)" = "$peer" ] || fail 'WireGuard peers changed'
    [ "$(wg show "$INTERFACE" listen-port)" = "$LOCAL_WG_PORT" ] || fail 'WireGuard UDP port changed'
    [ "$(wg show "$INTERFACE" fwmark)" = off ] || fail 'WireGuard policy routing is forbidden'
    [ "$(wg show "$INTERFACE" allowed-ips)" = "$peer"$'\t'"$PEER_WG/32" ] || fail 'WireGuard AllowedIPs changed'
    [ "$(wg show "$INTERFACE" persistent-keepalive)" = "$peer"$'\t'25 ] || fail 'WireGuard keepalive changed'
    [ "$(wg show "$INTERFACE" endpoints)" = "$peer"$'\t'"$PEER_PUBLIC:$PEER_WG_PORT" ] || fail 'WireGuard peer endpoint changed'
    psk=$(awk '/^[[:space:]]*PresharedKey[[:space:]]*=/ {sub(/^[^=]*=[[:space:]]*/,""); print}' "$NODE/wg.conf" | sha256sum)
    actual_psk=$(wg show "$INTERFACE" preshared-keys | awk '{print $2}' | sha256sum)
    [ "$psk" = "$actual_psk" ] || fail 'WireGuard preshared key changed'
    ip -o -4 addr show dev "$INTERFACE" | awk -v wanted="$LOCAL_WG/32" '
        {if($4==wanted) n++; else bad=1} END {exit !(n==1 && !bad)}' || fail 'WireGuard address changed'
    ip -o link show dev "$INTERFACE" | awk '{for(i=1;i<NF;i++) if($i=="mtu" && $(i+1)==1380) ok=1} END {exit !ok}' || fail 'WireGuard MTU changed'
    ip -4 route get "$PEER_WG" | awk -v dev="$INTERFACE" -v src="$LOCAL_WG" '
        {for(i=1;i<NF;i++){if($i=="dev" && $(i+1)==dev) d=1; if($i=="src" && $(i+1)==src) s=1}} END {exit !(d&&s)}' || fail 'Tunnel peer route changed'
    # RU actively probes the EU private inbound; EU has no inbound service on
    # RU to probe. PersistentKeepalive initiates an authenticated exchange for
    # both roles. Wait only in verify, after both installs have completed.
    if [ "$ROLE" = ru ]; then probe_tcp "$PEER_WG" 2443 || true; fi
    local deadline=$((SECONDS+35))
    while :; do
        handshake=$(wg show "$INTERFACE" latest-handshakes | awk -v peer="$peer" '$1==peer {print $2}')
        now=$(date +%s)
        if [[ "$handshake" =~ ^[0-9]+$ ]] && (( handshake>0 && handshake<=now && now-handshake<=180 )); then break; fi
        (( SECONDS < deadline )) || fail 'No authenticated WireGuard handshake within 180 seconds; both roles must be installed and UDP reachable'
        sleep 1
    done
}

case "$ACTION" in
identity)
    [ -f /etc/machine-id ] && [ ! -L /etc/machine-id ] || fail 'Machine identity missing'
    [ -f /etc/ssh/ssh_host_ed25519_key.pub ] && [ ! -L /etc/ssh/ssh_host_ed25519_key.pub ] || fail 'ED25519 host key missing'
    machine=$(tr -d '\n' < /etc/machine-id)
    fingerprint=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256 | awk '{print $2}')
    [[ "$machine" =~ ^[0-9a-f]{32}$ && "$fingerprint" =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]] || fail 'Invalid machine identity'
    if owned_node; then managed=true; else managed=false; fi
    printf '{"machineId":"%s","sshFingerprint":"%s","ownedInstallation":%s}\n' "$machine" "$fingerprint" "$managed"
    ;;
preflight)
    target_network
    if [ -e "$NODE" ] || [ -L "$NODE" ]; then
        owned_node || fail 'Existing cascade is not owned by this deployment and role'
        owned_parameters
        owned_listeners_safe || fail 'An unrelated public service exists on the owned guest'
    fi
    for legacy in /opt/spn-reality-node /opt/spn-direct-node /opt/remnanode /opt/remnanode-single75; do
        [ ! -e "$legacy" ] && [ ! -L "$legacy" ] || fail 'An older node installation exists; use a clean VPS'
    done
    check_containers
    check_wireguard_ownership
    routes_safe
    if ! owned_node; then
        fresh_listeners_safe || fail 'An unrelated public TCP or UDP listener exists; use a clean VPS'
        fresh_firewall_safe
        unowned_configs_absent
    elif [ -e "$NODE/firewall.sha256" ] || [ -L "$NODE/firewall.sha256" ]; then
        firewall_verified
    fi
    if [ -e "$STAGE/preflight-ok" ] || [ -L "$STAGE/preflight-ok" ]; then
        safe_file "$STAGE/preflight-ok"
        [ "$(cat "$STAGE/preflight-ok")" = "$PARAMS" ] || fail 'Saved target parameters differ'
    else printf '%s\n' "$PARAMS" > "$STAGE/preflight-ok"; fi
    printf 'Cascade %s preflight OK\n' "$ROLE"
    ;;
install)
    target_network
    preferred_source_safe
    safe_file "$STAGE/preflight-ok"
    [ "$(cat "$STAGE/preflight-ok")" = "$PARAMS" ] || fail 'Preflight parameters differ'
    validate_role_files
    if [ -e "$NODE" ] || [ -L "$NODE" ]; then
        owned_node || fail 'Existing cascade ownership differs'
        owned_parameters
        owned_listeners_safe || fail 'An unrelated public service exists on the owned guest'
    fi
    check_containers; check_wireguard_ownership; routes_safe
    if ! owned_node; then
        # Repeat safety checks immediately before the first mutation as well as
        # preflight; another service must not have appeared in between.
        fresh_listeners_safe || fail 'A public service appeared after preflight'
        fresh_firewall_safe
        unowned_configs_absent
        claim=$(mktemp -d /opt/.spn-cascade-claim.XXXXXXXX)
        install -m 0600 "$STAGE/.spn-deployment" "$claim/.spn-deployment"
        printf '%s\n' "$PARAMS" > "$claim/.spn-parameters"
        mv -Tn "$claim" "$NODE"
        if [ -d "$claim" ]; then rm "$claim/.spn-deployment" "$claim/.spn-parameters"; rmdir "$claim"; fi
    fi
    owned_node || fail 'Could not claim cascade ownership'
    install_owned "$STAGE/wg.conf" "$NODE/wg.conf"
    if [ "$ROLE" = ru ]; then install_owned "$STAGE/haproxy.cfg" "$NODE/haproxy.cfg"
    else
        install_owned "$STAGE/node.env" "$NODE/node.env"
        install_owned "$STAGE/docker-compose.yml" "$NODE/docker-compose.yml"
    fi
    install_packages
    if [ ! -e /etc/wireguard ]; then install -d -m 0700 /etc/wireguard; fi
    safe_dir /etc/wireguard
    install_owned "$NODE/wg.conf" "$WG_FILE"
    if [ "$ROLE" = ru ]; then
        haproxy -c -f "$NODE/haproxy.cfg" >/dev/null || fail 'HAProxy configuration validation failed'
        write_proxy_unit
    else
        validate_compose
        write_docker_unit
    fi
    install_firewall
    systemctl daemon-reload
    systemctl enable --now "wg-quick@$INTERFACE.service"
    if [ "$ROLE" = ru ]; then systemctl enable --now spn-cascade-haproxy.service
    else
        systemctl enable --now docker
        if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then docker pull "$IMAGE"; fi
        docker compose -f "$NODE/docker-compose.yml" up -d --pull never
    fi
    touch "$NODE/install-ok"
    printf 'Cascade %s installed; verify after both roles are installed\n' "$ROLE"
    ;;
verify)
    target_network; preferred_source_safe
    assert_owned_configs
    owned_listeners_safe || fail 'An unrelated public service exists on the owned guest'
    validate_role_files
    cmp -s "$STAGE/wg.conf" "$NODE/wg.conf" || fail 'Staged WireGuard configuration changed'
    check_containers; check_wireguard_ownership; routes_safe
    firewall_verified
    wireguard_runtime_verified
    if [ "$ROLE" = ru ]; then
        cmp -s "$STAGE/haproxy.cfg" "$NODE/haproxy.cfg" || fail 'Staged proxy configuration changed'
        haproxy -c -f "$NODE/haproxy.cfg" >/dev/null || fail 'HAProxy configuration is invalid'
        systemctl is-enabled --quiet spn-cascade-haproxy.service && systemctl is-active --quiet spn-cascade-haproxy.service || fail 'Owned HAProxy is not enabled and active'
        listener_exact 443 "$LOCAL_PUBLIC" || fail 'RU TCP443 must bind only the selected public IPv4'
        no_listener 2222 && no_listener 2443 || fail 'RU must not expose node API or VLESS core'
        probe_tcp "$PEER_WG" 2443 || fail 'EU private inbound is unreachable through WireGuard'
    else
        cmp -s "$STAGE/node.env" "$NODE/node.env" && cmp -s "$STAGE/docker-compose.yml" "$NODE/docker-compose.yml" || fail 'Staged EU configuration changed'
        validate_compose
        systemctl is-enabled --quiet docker.service && systemctl is-active --quiet docker.service || fail 'Owned Docker is not enabled and active'
        [ "$(docker inspect -f '{{.State.Running}}' remnanode)" = true ] || fail 'EU node is stopped'
        [ "$(docker inspect -f '{{range .HostConfig.Ulimits}}{{if eq .Name "nofile"}}{{.Soft}}:{{.Hard}}{{end}}{{end}}' remnanode)" = 65536:65536 ] || fail 'EU node nofile limits changed'
        no_listener 443 || fail 'EU must not have a public subscription listener on TCP443'
        listener_exact 2443 "$LOCAL_WG" || fail 'EU VLESS inbound must bind only its WireGuard IPv4'
        probe_tcp "$LOCAL_WG" 2443 || fail 'EU private inbound is not ready'
        # API may use a Go IPv6 wildcard; prove actual IPv4 readiness locally.
        probe_tcp "$LOCAL_PUBLIC" 2222 || fail 'EU panel API is not IPv4-ready'
    fi
    printf 'Cascade %s ownership, private transport, exact firewall and listeners verified; client app test still required\n' "$ROLE"
    ;;
esac
