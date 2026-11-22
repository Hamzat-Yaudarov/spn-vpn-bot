"""Pure, strict templates for the reviewed DE11 Yandex XHTTP transport.

Only extract_source performs I/O, and only GETs the two infrastructure lists.
No subscriber records, keys, credentials, DNS or server files are read here.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import re
import uuid
from urllib.parse import urlsplit


IMAGE = 'remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e'
SOURCE_HOST = 'c1614c56-403d-4a67-b917-928a04600db8'
SOURCE_PROFILE = '59aba40b-9ed3-4eec-b6af-b34a39da88d1'
SOURCE_INBOUND = '3f4c7e56-8784-4783-a34a-27dd1123c7a8'
SOURCE_TAG = 'vless-xhttp-yandex-de11'
SOURCE_PATH = '/assets/app/'
FINGERPRINT = 'firefox'

SHARED = {
    'xmux': {'cMaxReuseTimes': '0', 'maxConnections': '1', 'hKeepAlivePeriod': 0,
             'hMaxRequestTimes': '300-600', 'hMaxReusableSecs': '900-1800'},
    'noSSEHeader': False, 'xPaddingKey': '_token', 'uplinkDataKey': 'X-Transport-Data',
    'xPaddingBytes': '16-64', 'xPaddingHeader': 'X-Signature', 'xPaddingMethod': 'tokenish',
    'uplinkChunkSize': '2048', 'uplinkHTTPMethod': 'GET', 'xPaddingObfsMode': True,
    'xPaddingPlacement': 'query', 'scMaxBufferedPosts': 50, 'uplinkDataPlacement': 'header',
    'scMinPostsIntervalMs': '0-1', 'scStreamUpServerSecs': '60-180', 'serverMaxHeaderBytes': 8192,
}
SERVER = {'host': '', 'mode': 'packet-up', 'path': SOURCE_PATH, **SHARED, 'scMaxEachPostBytes': '2048'}
CLIENT = {**SHARED, 'scMaxEachPostBytes': '1024-2048'}


class ConfigError(ValueError):
    """Safe errors deliberately exclude untrusted values and configurations."""


def need(condition, message):
    if not condition:
        raise ConfigError(message)


def _ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (TypeError, ValueError):
        raise ConfigError('A canonical public IPv4 address is required.') from None
    need(isinstance(value, str) and str(address) == value and address.is_global and not address.is_multicast,
         'A canonical public IPv4 address is required.')


def _domain(value):
    need(isinstance(value, str) and 3 <= len(value) <= 253 and value == value.lower()
         and len(value.split('.')) >= 2 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x)
                                               for x in value.split('.')), 'A normalized DNS hostname is required.')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ConfigError('A DNS name, not an IP address, is required.')


def _uuid(value):
    try:
        need(isinstance(value, str) and str(uuid.UUID(value)) == value, 'A canonical UUID is required.')
    except (TypeError, ValueError, AttributeError):
        raise ConfigError('A canonical UUID is required.') from None


def validate_plan(plan):
    fields = {'format', 'mode', 'id', 'code', 'ip', 'sshPort', 'panel', 'panelIp', 'name', 'country',
              'domain', 'cdnDomain', 'dns', 'path', 'squads'}
    need(isinstance(plan, dict) and set(plan) == fields and type(plan['format']) is int and plan['format'] == 1
         and plan['mode'] == 'yandex-cdn', 'Unsupported Yandex CDN plan fields or format.')
    need(isinstance(plan['id'], str) and re.fullmatch('[0-9a-f]{32}', plan['id']), 'Invalid deployment identity.')
    need(isinstance(plan['code'], str) and re.fullmatch('[a-z0-9]{6}', plan['code']), 'Invalid deployment code.')
    _ip(plan['ip']); _ip(plan['panelIp'])
    need(plan['ip'] != plan['panelIp'], 'The node must be separate from the panel.')
    need(type(plan['sshPort']) is int and 1 <= plan['sshPort'] <= 65535
         and plan['sshPort'] not in (80, 443, 2222, 10050, 10085), 'SSH conflicts with a managed listener.')
    need(isinstance(plan['name'], str) and 1 <= len(plan['name']) <= 60
         and not any(ord(x) < 32 or ord(x) == 127 for x in plan['name']), 'Invalid display name.')
    need(isinstance(plan['country'], str) and (plan['country'] == '' or re.fullmatch('[A-Z]{2}', plan['country'])),
         'Invalid country code.')
    try:
        p = urlsplit(plan['panel'])
        valid_panel = (isinstance(plan['panel'], str) and p.scheme == 'https' and p.hostname
                       and p.port in (None, 443) and p.path == '/api' and not p.username and not p.password
                       and not p.query and not p.fragment and not any(x.isspace() for x in plan['panel']))
    except (ValueError, TypeError, AttributeError):
        valid_panel = False
    need(valid_panel, 'The panel must use an HTTPS /api URL without credentials.')
    dns = plan['dns']
    need(isinstance(dns, dict) and set(dns) == {'provider', 'zone', 'ttl'} and dns['provider'] == 'cloudflare'
         and type(dns['ttl']) is int and dns['ttl'] == 60, 'Unexpected DNS policy.')
    zone, origin, edge = (_domain(x) for x in (dns['zone'], plan['domain'], plan['cdnDomain']))
    need(origin != edge and all(name != zone and name.endswith('.' + zone) for name in (origin, edge)),
         'Use separate origin and CDN subdomains inside the selected zone.')
    need(isinstance(plan['path'], str) and re.fullmatch(r'/assets/edge-[0-9a-f]{16}/', plan['path']),
         'Invalid private XHTTP path.')
    squads = plan['squads']
    need(isinstance(squads, list) and 1 <= len(squads) <= 128
         and all(isinstance(x, str) for x in squads) and len(squads) == len(set(squads)),
         'Choose at least one unique squad UUID.')
    for identifier in squads:
        _uuid(identifier)
    return plan


def _source(source):
    def same(value, expected):
        try:
            return json.dumps(value, sort_keys=True, separators=(',', ':')) == json.dumps(expected, sort_keys=True, separators=(',', ':'))
        except (TypeError, ValueError):
            return False
    need(isinstance(source, dict) and set(source) == {'server', 'client', 'fingerprint', 'extraEncoding', 'sourceHost'}
         and same(source['server'], SERVER) and same(source['client'], CLIENT) and source['fingerprint'] == FINGERPRINT
         and source['extraEncoding'] in ('object', 'json-string') and source['sourceHost'] == SOURCE_HOST,
         'The source no longer matches the reviewed DE11 GET/header transport.')
    return source


def _one(rows, identifier):
    selected = [row for row in rows if isinstance(row, dict) and row.get('uuid') == identifier]
    need(len(selected) == 1, 'The source infrastructure object is missing or duplicated.')
    return selected[0]


def _parsed(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, 'Duplicate XHTTP source fields.')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError):
        raise ConfigError('Invalid source client XHTTP JSON.') from None


def extract_source(api):
    """Read infrastructure only; never return other inbounds or subscriber data."""
    if hasattr(api, 'rows'):
        hosts, profiles = api.rows('hosts'), api.rows('config-profiles')
    else:
        hosts, profiles = api('GET', '/hosts'), api('GET', '/config-profiles')['configProfiles']
    host = _one(hosts, SOURCE_HOST)
    need(host.get('inbound') == {'configProfileUuid': SOURCE_PROFILE, 'configProfileInboundUuid': SOURCE_INBOUND},
         'The source host binding changed.')
    selected = _one(profiles, SOURCE_PROFILE)
    meta = _one(selected.get('inbounds', []), SOURCE_INBOUND)
    need(meta.get('tag') == SOURCE_TAG, 'The source inbound tag changed.')
    items = [x for x in selected.get('config', {}).get('inbounds', []) if isinstance(x, dict) and x.get('tag') == SOURCE_TAG]
    need(len(items) == 1, 'The source transport is missing or duplicated.')
    inbound, extra = items[0], host.get('xhttpExtraParams')
    encoding = 'json-string' if isinstance(extra, str) else 'object'
    client = _parsed(extra) if isinstance(extra, str) else copy.deepcopy(extra)
    stream = inbound.get('streamSettings', {})
    need(inbound.get('protocol') == 'vless' and inbound.get('listen') == '127.0.0.1' and inbound.get('port') == 10085
         and inbound.get('settings') == {'clients': [], 'decryption': 'none'}
         and set(stream) == {'network', 'security', 'xhttpSettings'} and stream.get('network') == 'xhttp'
         and stream.get('security') == 'none', 'The source is no longer an isolated loopback XHTTP listener.')
    need(host.get('port') == 443 and host.get('securityLayer') == 'TLS' and host.get('alpn') == 'h2'
         and host.get('fingerprint') == FINGERPRINT and host.get('path') == SOURCE_PATH
         and host.get('address') == host.get('sni') == host.get('host')
         and not host.get('overrideSniFromAddress') and not host.get('keepSniBlank'),
         'The source edge TLS endpoint policy changed.')
    _domain(host['address'])
    return _source({'server': copy.deepcopy(stream.get('xhttpSettings')), 'client': client,
                    'fingerprint': FINGERPRINT, 'extraEncoding': encoding, 'sourceHost': SOURCE_HOST})


def profile(plan, source):
    validate_plan(plan); _source(source)
    server = copy.deepcopy(source['server']); server['path'] = plan['path']
    return {
        'log': {'loglevel': 'warning', 'access': 'none'},
        'dns': {'servers': ['localhost'], 'queryStrategy': 'UseIPv4'},
        'inbounds': [{'tag': 'yandex-xhttp-' + plan['code'], 'listen': '127.0.0.1', 'port': 10085, 'protocol': 'vless',
                      'settings': {'clients': [], 'decryption': 'none'},
                      'streamSettings': {'network': 'xhttp', 'security': 'none', 'xhttpSettings': server}}],
        'policy': {'levels': {'0': {'handshake': 4, 'connIdle': 120, 'uplinkOnly': 5, 'downlinkOnly': 5}}},
        'outbounds': [{'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
                      {'tag': 'block', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'IPIfNonMatch', 'rules': [{'type': 'field',
                    'ip': ['geoip:private', '169.254.0.0/16', '168.63.129.16/32', '100.64.0.0/10',
                           '224.0.0.0/4', 'ff00::/8'], 'outboundTag': 'block'}]},
    }


def host_body(plan, source, profileUuid, nodeUuid, inboundUuid):
    validate_plan(plan); _source(source)
    for value in (profileUuid, nodeUuid, inboundUuid):
        _uuid(value)
    client = copy.deepcopy(source['client'])
    extra = json.dumps(client, separators=(',', ':')) if source['extraEncoding'] == 'json-string' else client
    return {'remark': plan['name'] + ' • ОБХОД CDN ' + plan['code'], 'address': plan['cdnDomain'], 'port': 443,
            'sni': plan['cdnDomain'], 'host': plan['cdnDomain'], 'path': plan['path'], 'alpn': 'h2',
            'fingerprint': source['fingerprint'], 'securityLayer': 'TLS', 'xhttpExtraParams': extra,
            'nodes': [nodeUuid], 'inbound': {'configProfileUuid': profileUuid, 'configProfileInboundUuid': inboundUuid},
            'isDisabled': True, 'isHidden': False, 'internalSquads': {'mode': 'EXCLUDE', 'squads': []},
            'mihomoX25519': False, 'overrideSniFromAddress': False, 'keepSniBlank': False,
            'tags': ['AUTO_YANDEX_CDN']}


def _bootstrap(plan):
    ip, origin = plan['ip'], plan['domain']
    return f'''server {{
    listen {ip}:80 default_server;
    server_name _;
    return 444;
}}
server {{
    listen {ip}:80;
    server_name {origin};
    location ^~ /.well-known/acme-challenge/ {{
        root /var/www/spn-yandex-acme;
        try_files $uri =404;
    }}
    location = /health {{
        default_type application/json;
        add_header Cache-Control no-store always;
        return 503 '{{"status":"preparing","deployment":"{plan['code']}"}}\n';
    }}
    location / {{ return 503; }}
}}
'''


def _nginx(plan):
    ip, origin = plan['ip'], plan['domain']
    return f'''server {{
    listen {ip}:80 default_server;
    server_name _;
    return 444;
}}
server {{
    listen {ip}:443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
}}
server {{
    listen {ip}:80;
    server_name {origin};
    location ^~ /.well-known/acme-challenge/ {{
        root /var/www/spn-yandex-acme;
        try_files $uri =404;
    }}
    location / {{ return 301 https://{origin}$request_uri; }}
}}
server {{
    listen {ip}:443 ssl http2;
    server_name {origin};
    ssl_certificate /etc/letsencrypt/live/{origin}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{origin}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:SPNYANDEX:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    server_tokens off;
    client_header_buffer_size 8k;
    large_client_header_buffers 4 8k;
    if ($host != {origin}) {{ return 444; }}
    location ^~ {plan['path']} {{
        client_max_body_size 2m;
        proxy_pass http://127.0.0.1:10085;
        proxy_http_version 1.1;
        proxy_set_header Host {origin};
        proxy_set_header Connection "";
        proxy_set_header Upgrade "";
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Forwarded "";
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        add_header Cache-Control no-store always;
        access_log off;
    }}
    location = /health {{
        default_type application/json;
        add_header Cache-Control no-store always;
        return 200 '{{"status":"ok","deployment":"{plan['code']}"}}\n';
    }}
    location / {{ return 404; }}
}}
'''


def role_files(plan, node_secret):
    validate_plan(plan)
    need(isinstance(node_secret, str) and 20 <= len(node_secret) <= 8192
         and re.fullmatch(r'[A-Za-z0-9_/+=-]+', node_secret), 'Invalid node service secret.')
    main = '''user www-data;
worker_processes auto;
worker_rlimit_nofile 65535;
pid /run/nginx.pid;
error_log /var/log/nginx/error.log warn;
include /etc/nginx/modules-enabled/*.conf;
events { worker_connections 8192; }
http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    sendfile on;
    tcp_nopush on;
    types_hash_max_size 2048;
    server_tokens off;
    access_log off;
    keepalive_timeout 65;
    gzip off;
    include /etc/nginx/sites-enabled/*;
}
'''
    compose = f'''services:
  remnanode:
    container_name: remnanode
    hostname: remnanode
    image: {IMAGE}
    restart: unless-stopped
    network_mode: host
    labels:
      spn.deploy.id: "{plan['id']}"
      spn.deploy.mode: "yandex-cdn"
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
    return {'.spn-deployment': (plan['id'] + '\n').encode('ascii'),
            'node.env': ('NODE_PORT=2222\nSECRET_KEY=' + node_secret + '\n').encode('ascii'),
            'docker-compose.yml': compose.encode('ascii'), 'bootstrap.conf': _bootstrap(plan).encode('ascii'),
            'nginx.conf': _nginx(plan).encode('ascii'), 'nginx-main.conf': main.encode('ascii'),
            'renew-nginx.sh': b'#!/bin/sh\nset -eu\n/usr/sbin/nginx -t\n/usr/bin/systemctl reload nginx.service\n'}
