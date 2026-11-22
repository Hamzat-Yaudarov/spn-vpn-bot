"""Pure configuration for a fail-closed RU -> encrypted link -> EU cascade.

Only ``wg_keys`` writes local private state, through the installer's immutable
Store interface. Renderers never contact the panel, DNS, or either server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import uuid
from urllib.parse import urlsplit


IMAGE = 'remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e'
SNI = 'www.cloudflare.com'
FINGERPRINT = 'safari'
WG_STATE = 'wireguard.private.json'
LINK_POOL = ipaddress.IPv4Network('10.253.0.0/16')


class ConfigError(ValueError):
    """Invalid or changed configuration; never include secret values in errors."""


def _need(condition, message):
    if not condition:
        raise ConfigError(message)


def _keys(value, expected, message):
    _need(isinstance(value, dict) and set(value) == set(expected), message)


def _integer(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _public_ipv4(value):
    _need(isinstance(value, str), 'A public IPv4 address is required.')
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ConfigError('A public IPv4 address is required.') from None
    _need(address.is_global and not address.is_multicast and str(address) == value,
          'A public IPv4 address is required.')
    return address


def _domain(value):
    _need(isinstance(value, str) and 3 <= len(value) <= 253 and value == value.lower(),
          'A normalized DNS hostname is required.')
    labels = value.split('.')
    _need(len(labels) >= 2 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                 for label in labels), 'A normalized DNS hostname is required.')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ConfigError('A DNS hostname, not an IP address, is required.')


def generate_link(deployment_id, code):
    """Derive one stable /30 pair; interfaces and routes still use only /32.

    There are 16,384 possible pairs in the reserved deployment pool. A remote
    preflight must reject an existing conflicting interface/route, not adopt it.
    """
    _need(isinstance(deployment_id, str) and re.fullmatch(r'[0-9a-f]{32}', deployment_id),
          'Invalid deployment identity.')
    _need(isinstance(code, str) and re.fullmatch(r'[a-z0-9]{6}', code), 'Invalid deployment code.')
    digest = hashlib.sha256(deployment_id.encode('ascii')).digest()
    offset = (int.from_bytes(digest[:2], 'big') % 16384) * 4
    network = int(LINK_POOL.network_address) + offset
    return {'interface': 'spnwg' + code, 'ru': str(ipaddress.IPv4Address(network + 1)),
            'eu': str(ipaddress.IPv4Address(network + 2)), 'prefix': 32, 'port': 51820, 'mtu': 1380}


def entry_port(plan):
    """Public TCP entry; format-1 plans remain fixed on 443."""
    return plan.get('entryPort', 443)


def ru_wg_port(plan):
    """Branch-specific RU UDP listener; format 1 uses the original EU port."""
    return plan['link'].get('ruPort', plan['link']['port'])


def _branch_port(value, ssh_port):
    return (_integer(value, 1024, 65535) or type(value) is int and value == 443) and value not in (
        2222, 2443, ssh_port)


def validate_plan(plan):
    """Validate the complete public plan before any value enters a config file."""
    _need(isinstance(plan, dict) and type(plan.get('format')) is int and plan['format'] in (1, 2),
          'Unsupported cascade plan format or mode.')
    fields = {'format', 'id', 'code', 'ru', 'eu', 'panel', 'panelIp', 'name', 'country',
              'domain', 'dns', 'link', 'squads', 'mode'}
    if plan['format'] == 2:
        fields.add('entryPort')
    _keys(plan, fields, 'Unexpected or missing plan fields.')
    _need(plan['mode'] == 'cascade-wg',
          'Unsupported cascade plan format or mode.')
    _need(isinstance(plan['id'], str) and re.fullmatch(r'[0-9a-f]{32}', plan['id']),
          'Invalid deployment identity.')
    _need(isinstance(plan['code'], str) and re.fullmatch(r'[a-z0-9]{6}', plan['code']),
          'Invalid deployment code.')
    for role in ('ru', 'eu'):
        _keys(plan[role], {'ip', 'sshPort'}, 'Invalid server fields.')
        _public_ipv4(plan[role]['ip'])
        _need(_integer(plan[role]['sshPort'], 1, 65535), 'Invalid SSH port.')
    _need(plan['ru']['ip'] != plan['eu']['ip'], 'The cascade requires two different public servers.')
    _need(all(plan[role]['sshPort'] not in (443, 2222, 2443) for role in ('ru', 'eu')),
          'The SSH port conflicts with a role listener.')
    if plan['format'] == 2:
        _need(_branch_port(plan['entryPort'], plan['ru']['sshPort']),
              'Invalid or conflicting branch TCP entry port.')
    _public_ipv4(plan['panelIp'])
    _need(plan['panelIp'] not in (plan['ru']['ip'], plan['eu']['ip']),
          'The existing panel must be separate from the clean cascade servers.')
    _need(isinstance(plan['panel'], str), 'Invalid panel API URL.')
    try:
        panel = urlsplit(plan['panel'])
        valid_panel = (panel.scheme == 'https' and bool(panel.hostname) and panel.port in (None, 443)
                       and panel.path == '/api' and not panel.query and not panel.fragment
                       and panel.username is None and panel.password is None
                       and not any(character.isspace() for character in plan['panel']))
    except ValueError:
        valid_panel = False
    _need(valid_panel, 'The panel API must be an HTTPS /api URL without credentials.')
    _need(isinstance(plan['name'], str) and 1 <= len(plan['name']) <= 128
          and not any(ord(character) < 32 or ord(character) == 127 for character in plan['name']),
          'Invalid display name.')
    _need(isinstance(plan['country'], str) and (plan['country'] == '' or
          re.fullmatch(r'[A-Z]{2}', plan['country'])), 'Invalid country code.')
    domain = _domain(plan['domain'])
    _keys(plan['dns'], {'provider', 'zone', 'ttl'}, 'Invalid DNS settings.')
    zone = _domain(plan['dns']['zone'])
    _need(plan['dns']['provider'] == 'cloudflare' and type(plan['dns']['ttl']) is int
          and plan['dns']['ttl'] == 60 and (domain == zone or domain.endswith('.' + zone)),
          'The endpoint must be a DNS-only Cloudflare name inside its saved zone.')
    link = plan['link']
    link_fields = {'interface', 'ru', 'eu', 'port', 'mtu'}
    if plan['format'] == 2:
        link_fields.add('ruPort')
    _need(isinstance(link, dict) and set(link) in (link_fields, link_fields | {'prefix'}),
        'Invalid WireGuard link fields.')
    if plan['format'] == 2:
        _need(_branch_port(link['ruPort'], plan['ru']['sshPort']),
              'Invalid or conflicting branch RU UDP port.')
        _need(_branch_port(link['port'], plan['eu']['sshPort']),
              'Invalid or conflicting branch EU UDP port.')
    else:
        _need(type(link['port']) is int and link['port'] == 51820,
              'Format-1 WireGuard requires the original UDP port 51820.')
    _need(link['interface'] == 'spnwg' + plan['code'] and link.get('prefix', 32) == 32
          and type(link.get('prefix', 32)) is int and type(link['mtu']) is int and link['mtu'] == 1380,
          'Unexpected WireGuard interface, prefix, port, or MTU.')
    try:
        ru_link = ipaddress.IPv4Address(link['ru'])
        eu_link = ipaddress.IPv4Address(link['eu'])
        block = ipaddress.IPv4Network(str(ru_link) + '/30', strict=False)
    except (ValueError, TypeError):
        raise ConfigError('Invalid private WireGuard addresses.') from None
    _need(isinstance(link['ru'], str) and isinstance(link['eu'], str)
          and ru_link in LINK_POOL and eu_link in LINK_POOL
          and ru_link == block.network_address + 1 and eu_link == block.network_address + 2,
          'WireGuard requires the two reserved host addresses in one deployment /30 pair.')
    squads = plan['squads']
    _need(isinstance(squads, list) and 1 <= len(squads) <= 128
          and all(isinstance(value, str) for value in squads), 'At least one squad UUID is required.')
    try:
        valid_squads = all(str(uuid.UUID(value)) == value for value in squads)
    except ValueError:
        valid_squads = False
    _need(valid_squads and len(squads) == len(set(squads)), 'Squad UUIDs must be canonical and unique.')
    return plan


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    except ImportError:
        raise ConfigError('Install the local key dependency: python3 -m pip install cryptography') from None
    return serialization, X25519PrivateKey


def _wg_decode(value):
    _need(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9+/]{43}=', value),
          'Invalid WireGuard key encoding.')
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        raise ConfigError('Invalid WireGuard key encoding.') from None
    _need(len(raw) == 32 and base64.b64encode(raw).decode('ascii') == value and any(raw),
          'Invalid WireGuard key encoding.')
    return raw


def _validate_wg(keys):
    _keys(keys, {'ru', 'eu', 'psk'}, 'Invalid private WireGuard state.')
    serialization, private_type = _crypto()
    for role in ('ru', 'eu'):
        _keys(keys[role], {'private', 'public'}, 'Invalid private WireGuard key pair.')
        private, public = _wg_decode(keys[role]['private']), _wg_decode(keys[role]['public'])
        actual = private_type.from_private_bytes(private).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        _need(hmac.compare_digest(actual, public), 'WireGuard private/public key mismatch.')
    _wg_decode(keys['psk'])
    _need(keys['ru']['private'] != keys['eu']['private'] and keys['ru']['public'] != keys['eu']['public'],
          'The two WireGuard peers must have different key pairs.')
    return keys


def wg_keys(store):
    """Create once in a secure immutable Store; reject damaged cached keys."""
    cached = store.load(WG_STATE)
    if cached is not None:
        return _validate_wg(cached)
    serialization, private_type = _crypto()
    keys = {}
    encode = lambda value: base64.b64encode(value).decode('ascii')
    for role in ('ru', 'eu'):
        private = private_type.generate()
        keys[role] = {
            'private': encode(private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                    serialization.NoEncryption())),
            'public': encode(private.public_key().public_bytes(serialization.Encoding.Raw,
                                                               serialization.PublicFormat.Raw)),
        }
    keys['psk'] = encode(secrets.token_bytes(32))
    _validate_wg(keys)
    store.save(WG_STATE, keys)
    return keys


def _wireguard(plan, role, keys):
    peer = 'eu' if role == 'ru' else 'ru'
    listen_port = ru_wg_port(plan) if role == 'ru' else plan['link']['port']
    endpoint_port = plan['link']['port'] if role == 'ru' else ru_wg_port(plan)
    return f'''# SPN encrypted cascade: {plan['id']} {role}
[Interface]
Address = {plan['link'][role]}/32
ListenPort = {listen_port}
PrivateKey = {keys[role]['private']}
MTU = {plan['link']['mtu']}

[Peer]
PublicKey = {keys[peer]['public']}
PresharedKey = {keys['psk']}
AllowedIPs = {plan['link'][peer]}/32
Endpoint = {plan[peer]['ip']}:{endpoint_port}
PersistentKeepalive = 25
'''


def _haproxy(plan):
    return f'''# SPN cascade {plan['id']}; opaque TCP passthrough, no TLS keys.
global
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
    bind {plan['ru']['ip']}:{entry_port(plan)}
    maxconn 8192
    tcp-request inspect-delay 5s
    acl complete_client_hello req.ssl_hello_type 1
    tcp-request content accept if complete_client_hello
    tcp-request content reject if WAIT_END
    default_backend spn_cascade_eu

backend spn_cascade_eu
    server eu {plan['link']['eu']}:2443 send-proxy-v2 maxconn 8192
'''


def _compose(plan):
    # Keep the node's existing writable runtime contract. It receives dynamic
    # Xray config from the panel; blindly making its filesystem read-only breaks
    # that operation. No host directories or Docker socket are mounted.
    return f'''services:
  remnanode:
    container_name: remnanode
    hostname: remnanode
    image: {IMAGE}
    restart: unless-stopped
    network_mode: host
    labels:
      spn.deploy.id: "{plan['id']}"
      spn.deploy.role: "eu"
    env_file:
      - node.env
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    logging:
      driver: json-file
      options:
        max-size: 20m
        max-file: '3'
'''


def role_files(plan, role, keys, *, node_secret=None):
    """Render just one server's files; no remote peer private key can cross roles."""
    validate_plan(plan)
    _need(role in ('ru', 'eu'), 'The role must be ru or eu.')
    _validate_wg(keys)
    if role == 'ru':
        _need(node_secret is None, 'The RU relay must not receive a node service secret.')
    else:
        _need(isinstance(node_secret, str) and 20 <= len(node_secret) <= 8192
              and re.fullmatch(r'[A-Za-z0-9_/+=-]+', node_secret), 'Invalid node service secret.')
    files = {'.spn-deployment': (plan['id'] + '|' + role + '\n').encode('ascii'),
             'wg.conf': _wireguard(plan, role, keys).encode('ascii')}
    if role == 'ru':
        files['haproxy.cfg'] = _haproxy(plan).encode('ascii')
    else:
        files['docker-compose.yml'] = _compose(plan).encode('ascii')
        files['node.env'] = ('NODE_PORT=2222\nSECRET_KEY=' + node_secret + '\n').encode('ascii')
    return files


def _reality_keys(keys):
    _keys(keys, {'private', 'public', 'short'}, 'Invalid private REALITY state.')
    serialization, private_type = _crypto()
    values = {}
    for name in ('private', 'public'):
        value = keys[name]
        _need(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', value),
              'Invalid REALITY key encoding.')
        raw = base64.urlsafe_b64decode(value + '=')
        _need(len(raw) == 32 and any(raw) and base64.urlsafe_b64encode(raw).decode().rstrip('=') == value,
              'Invalid REALITY key encoding.')
        values[name] = raw
    actual = private_type.from_private_bytes(values['private']).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    _need(hmac.compare_digest(actual, values['public']), 'REALITY private/public key mismatch.')
    _need(isinstance(keys['short'], str) and re.fullmatch(r'[0-9a-f]{16}', keys['short']),
          'Invalid REALITY short ID.')


def profile_config(plan, keys):
    """One EU-only VLESS REALITY/Vision listener, with EU-local IPv4 egress."""
    validate_plan(plan)
    _reality_keys(keys)
    return {
        'log': {'loglevel': 'warning', 'access': 'none'},
        'dns': {'servers': ['localhost'], 'queryStrategy': 'UseIPv4'},
        'inbounds': [{
            'tag': 'cascade-' + plan['code'], 'listen': plan['link']['eu'], 'port': 2443, 'protocol': 'vless',
            'settings': {'clients': [], 'decryption': 'none'},
            'sniffing': {'enabled': True, 'destOverride': ['http', 'tls'], 'routeOnly': True},
            'streamSettings': {
                'network': 'raw', 'security': 'reality',
                'sockopt': {'acceptProxyProtocol': True},
                'realitySettings': {'show': False, 'target': SNI + ':443', 'xver': 0,
                                    'serverNames': [SNI], 'privateKey': keys['private'],
                                    'shortIds': [keys['short']]},
            },
        }],
        'policy': {'levels': {'0': {'handshake': 4, 'connIdle': 120, 'uplinkOnly': 5, 'downlinkOnly': 5}}},
        'outbounds': [{'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
                      {'tag': 'block', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [{
            'type': 'field', 'ip': ['geoip:private', '169.254.0.0/16', '168.63.129.16/32', '100.64.0.0/10'],
            'outboundTag': 'block',
        }]},
    }
