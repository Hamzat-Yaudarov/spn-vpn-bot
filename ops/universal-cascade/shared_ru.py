#!/usr/bin/env python3
"""Serialized, additive RU branch management. Never reload an existing branch.

The registry contains public identities and immutable file digests, not keys.
Pending intents make interrupted additions roll-forward without adopting unknown
ports, firewall rules, routes, files or services.
"""
from __future__ import annotations

import base64
import configparser
import fcntl
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time


_checks_spec = importlib.util.spec_from_file_location('spn_cascade_host_checks', Path(__file__).with_name('host_checks.py'))
host_checks = importlib.util.module_from_spec(_checks_spec)
_checks_spec.loader.exec_module(host_checks)


HOME = '/opt/spn-cascade-ru'
LEGACY = '/opt/spn-cascade'
BASE_FILES = ['/etc/ufw/ufw.conf', '/etc/ufw/before.rules', '/etc/ufw/after.rules',
              '/etc/ufw/before6.rules', '/etc/ufw/after6.rules', '/etc/default/ufw']
FIELDS = {'id', 'entryPort', 'ruWGPort', 'euWGPort', 'ruIP', 'euIP', 'panelIP', 'sshPort',
          'interface', 'localWG', 'peerWG'}


class Error(RuntimeError):
    """Errors deliberately contain no configuration contents or credentials."""


def need(ok, message):
    if not ok:
        raise Error(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate(branch):
    need(isinstance(branch, dict) and set(branch) == FIELDS, 'Invalid branch identity fields')
    need(isinstance(branch['id'], str) and re.fullmatch('[0-9a-f]{32}', branch['id']), 'Invalid deployment ID')
    need(isinstance(branch['interface'], str) and re.fullmatch(r'spnwg[a-z0-9]{1,10}', branch['interface']),
         'Invalid owned WireGuard interface')
    for key in ('ruIP', 'euIP', 'panelIP'):
        try:
            address = ipaddress.IPv4Address(branch[key])
            need(address.is_global and not address.is_multicast and str(address) == branch[key], 'Invalid public IPv4')
        except (ValueError, TypeError):
            raise Error('Invalid public IPv4') from None
    need(len({branch[k] for k in ('ruIP', 'euIP', 'panelIP')}) == 3, 'Panel and branch hosts must differ')
    for key in ('localWG', 'peerWG'):
        try:
            address = ipaddress.IPv4Address(branch[key])
            need(any(address in ipaddress.IPv4Network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')),
                 'Invalid private tunnel IPv4')
        except (ValueError, TypeError):
            raise Error('Invalid private tunnel IPv4') from None
    need(branch['localWG'] != branch['peerWG'], 'Tunnel peers must differ')
    for key in ('entryPort', 'ruWGPort', 'euWGPort', 'sshPort'):
        need(type(branch[key]) is int and 1 <= branch[key] <= 65535, 'Invalid branch port')
    need(branch['entryPort'] not in (branch['sshPort'], 2222, 2443), 'Entry TCP port conflicts with management')
    need(branch['sshPort'] not in (2222, 2443), 'SSH TCP port conflicts with management')
    return branch


def params(branch):
    b = branch
    return '|'.join(map(str, ('ru', b['ruIP'], b['euIP'], b['panelIP'], b['sshPort'], b['id'],
                              b['interface'], b['localWG'], b['peerWG'], b['euWGPort'])))


def rules(branches):
    result = set()
    for b in branches:
        result.update({('tcp', str(b['sshPort']), 'any', 'any', ''),
                       ('tcp', str(b['entryPort']), 'any', 'any', ''),
                       ('udp', str(b['ruWGPort']), b['euIP'], 'any', '')})
    return result


def parse_rules(text):
    seen = []
    try:
        for line in text.splitlines():
            if not line.startswith('ufw '):
                continue
            tokens = shlex.split(line)[1:]
            if 'comment' in tokens:
                tokens = tokens[:tokens.index('comment')]
            need(tokens and tokens.pop(0) == 'allow', 'Unexpected firewall action')
            values = {'proto': None, 'port': None, 'from': 'any', 'to': 'any', 'on': ''}
            assigned = set()
            if tokens and tokens[0] == 'in':
                tokens.pop(0)
            while tokens:
                token = tokens.pop(0)
                if token in values:
                    need(tokens and token not in assigned, 'Unexpected firewall rule')
                    assigned.add(token)
                    values[token] = tokens.pop(0)
                elif '/' in token and values['port'] is None:
                    values['port'], values['proto'] = token.split('/')
                    assigned.update(('port', 'proto'))
                else:
                    raise Error('Unexpected firewall rule')
            for key in ('from', 'to'):
                if values[key].endswith('/32'):
                    values[key] = values[key][:-3]
                if values[key] == '0.0.0.0/0':
                    values[key] = 'any'
            seen.append(tuple(values[k] for k in ('proto', 'port', 'from', 'to', 'on')))
    except (ValueError, IndexError):
        raise Error('Unexpected firewall syntax') from None
    need(len(seen) == len(set(seen)), 'Duplicate firewall rules')
    return set(seen)


def proxy_text(b):
    return f'''global
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
bind {b['ruIP']}:{b['entryPort']}
maxconn 8192
tcp-request inspect-delay 5s
acl complete_client_hello req.ssl_hello_type 1
tcp-request content accept if complete_client_hello
tcp-request content reject if WAIT_END
default_backend spn_cascade_eu
backend spn_cascade_eu
server eu {b['peerWG']}:2443 send-proxy-v2 maxconn 8192'''


def normalize_proxy(data):
    return '\n'.join(' '.join(line.split()) for line in data.decode().splitlines()
                     if line.strip() and not line.lstrip().startswith('#'))


def validate_wg(data, b):
    try:
        p = configparser.ConfigParser(interpolation=None, strict=True)
        p.optionxform = str
        p.read_string(data.decode('ascii'))
        need(p.sections() == ['Interface', 'Peer'] and not p.defaults(), 'Unexpected tunnel sections')
        i, r = dict(p['Interface']), dict(p['Peer'])
        need(set(i) == {'Address', 'PrivateKey', 'ListenPort', 'MTU'} and
             set(r) == {'PublicKey', 'PresharedKey', 'AllowedIPs', 'Endpoint', 'PersistentKeepalive'},
             'Unexpected tunnel directives')
        need((i['Address'], i['ListenPort'], i['MTU']) == (b['localWG'] + '/32', str(b['ruWGPort']), '1380') and
             (r['AllowedIPs'], r['Endpoint'], r['PersistentKeepalive']) ==
             (b['peerWG'] + '/32', b['euIP'] + ':' + str(b['euWGPort']), '25'), 'Unexpected tunnel routes or ports')
        for key in (i['PrivateKey'], r['PublicKey'], r['PresharedKey']):
            raw = base64.b64decode(key, validate=True)
            need(len(raw) == 32 and any(raw) and base64.b64encode(raw).decode() == key, 'Invalid tunnel key')
        return i, r
    except (ValueError, UnicodeError, configparser.Error):
        raise Error('Invalid tunnel configuration') from None


def branch_dir(b):
    return LEGACY if b.get('legacy') else HOME + '/branches/' + b['id']


def unit_name(b):
    return 'spn-cascade-haproxy.service' if b.get('legacy') else 'spn-cascade-' + b['id'][:12] + '.service'


def unit_text(b):
    return ('[Unit]\nDescription=SPN cascade owned TCP passthrough\nWants=network-online.target\n'
            f'Requires=wg-quick@{b["interface"]}.service\nAfter=network-online.target wg-quick@{b["interface"]}.service\n'
            '[Service]\nType=notify\nExecStart=/usr/sbin/haproxy -Ws -f ' + branch_dir(b) + '/haproxy.cfg\n'
            'Restart=on-failure\nRestartSec=2\nLimitNOFILE=131072\nNoNewPrivileges=true\nProtectHome=true\n'
            'PrivateTmp=true\n[Install]\nWantedBy=multi-user.target\n').encode()


def collisions(branches, wanted):
    validate(wanted)
    for old in branches:
        identity = {k: old[k] for k in FIELDS}
        if old['id'] == wanted['id']:
            need(identity == wanted, 'Existing branch parameters changed')
            continue
        need(old['ruIP'] == wanted['ruIP'] and old['sshPort'] == wanted['sshPort'], 'Shared host identity differs')
        need(old['entryPort'] != wanted['entryPort'], 'Entry TCP port is already owned')
        need(old['ruWGPort'] != wanted['ruWGPort'], 'WireGuard UDP port is already owned')
        need(old['interface'] != wanted['interface'], 'WireGuard interface is already owned')
        need(old['id'][:12] != wanted['id'][:12], 'Branch unit name collision')
        need(not ({old['localWG'], old['peerWG']} & {wanted['localWG'], wanted['peerWG']}), 'Tunnel IPv4 collision')


class Manager:
    def __init__(self, branch, *, root=Path('/'), uid=0, runner=None, sleep=time.sleep):
        self.branch = validate(branch)
        self.root = Path(root)
        self.uid = uid
        self.runner = runner
        self.sleep = sleep
        self.stage = '/root/spn-cascade-managed-' + branch['id']
        self.phase = 'bootstrap'
        self.monitoring_ports = []

    def path(self, value):
        return self.root / value.lstrip('/')

    def directory(self, value, create=False):
        path = self.path(value)
        if create and not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o700)
        need(path.is_dir() and not path.is_symlink(), 'Unsafe owned directory')
        s = path.stat()
        need(s.st_uid == self.uid and stat.S_IMODE(s.st_mode) == 0o700, 'Unsafe owned directory metadata')

    def read(self, value):
        path = self.path(value)
        need(path.is_file() and not path.is_symlink(), 'Missing or unsafe owned file')
        s = path.stat()
        need(s.st_uid == self.uid and stat.S_IMODE(s.st_mode) == 0o600 and s.st_nlink == 1,
             'Unsafe owned file metadata')
        return path.read_bytes()

    def atomic(self, value, data):
        path = self.path(value)
        if path.exists() or path.is_symlink():
            self.read(value)
        fd, name = tempfile.mkstemp(prefix='.spn-write-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(name, path)
            self.sync_directory(path.parent)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def immutable(self, value, data):
        path = self.path(value)
        if path.exists() or path.is_symlink():
            need(self.read(value) == data, 'Existing managed file differs')
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            self.sync_directory(path.parent)

    @staticmethod
    def sync_directory(path):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def run(self, *args, data=None, optional=False):
        if self.runner:
            return self.runner(tuple(args), data=data, optional=optional)
        try:
            process = subprocess.run(args, input=data, capture_output=True, text=True)
        except FileNotFoundError:
            if optional:
                return None
            raise Error('Required host command is missing: ' + args[0]) from None
        if process.returncode and not optional:
            raise Error('Host command failed: ' + args[0])
        return process.stdout if not process.returncode else None

    def legacy(self):
        if not self.path(LEGACY).exists() and not self.path(LEGACY).is_symlink():
            return []
        self.directory(LEGACY)
        try:
            proof = json.loads(self.read(self.stage + '/shared-ru-proof.json'))
            need(isinstance(proof, dict) and set(proof) == {'branches'} and isinstance(proof['branches'], list),
                 'Invalid legacy ownership proof')
            marker = self.read(LEGACY + '/.spn-deployment').decode().strip()
            matches = [validate(x) for x in proof['branches'] if isinstance(x, dict) and isinstance(x.get('id'), str)
                       and x['id'] + '|ru' == marker]
            need(len(matches) == 1, 'Locally known legacy ownership proof is required')
            b = dict(matches[0], legacy=True, status='active')
            need((b['entryPort'], b['ruWGPort'], b['euWGPort']) == (443, 51820, 51820), 'Unexpected legacy ports')
            need(self.read(LEGACY + '/.spn-parameters').decode().strip() == params(b), 'Legacy parameters differ from local proof')
            wg = self.read(LEGACY + '/wg.conf'); validate_wg(wg, b)
            proxy = self.read(LEGACY + '/haproxy.cfg')
            need(normalize_proxy(proxy) == proxy_text(b), 'Unexpected legacy proxy configuration')
            need(self.read(LEGACY + '/haproxy.service') == unit_text(b), 'Unexpected legacy proxy unit')
            need(self.read('/etc/systemd/system/' + unit_name(b)) == unit_text(b), 'Legacy live service differs')
            need(self.read('/etc/wireguard/' + b['interface'] + '.conf') == wg, 'Legacy live tunnel differs')
            # The old manifest is valid only before union rules are added. It is
            # not rewritten: the new host ledger takes ownership of the union.
            self.run('sha256sum', '--check', '--status', LEGACY + '/firewall.sha256')
            b['files'] = self.file_hashes(b, wg, proxy)
            return [b]
        except (ValueError, UnicodeError, TypeError, KeyError):
            raise Error('Invalid legacy ownership proof') from None

    def file_hashes(self, b, wg, proxy):
        directory = branch_dir(b)
        files = {directory + '/wg.conf': digest(wg), directory + '/haproxy.cfg': digest(proxy),
                 directory + '/haproxy.service': digest(unit_text(b)),
                 '/etc/wireguard/' + b['interface'] + '.conf': digest(wg),
                 '/etc/systemd/system/' + unit_name(b): digest(unit_text(b))}
        if b.get('legacy'):
            for name in ('.spn-deployment', '.spn-parameters', 'firewall.sha256'):
                files[directory + '/' + name] = digest(self.read(directory + '/' + name))
        return files

    def load(self):
        if not self.path(HOME).exists() and not self.path(HOME).is_symlink():
            legacy = self.legacy()
            # The legacy manifest was verified immediately above. Preserve its
            # immutable firewall-base protection even if this run only resumes
            # that active branch and never adds another one.
            return {'version': 1, 'branches': legacy,
                    'baseline': self.base_hashes() if legacy else {}, 'initialized': bool(legacy)}
        self.directory(HOME); self.directory(HOME + '/branches')
        try:
            state = json.loads(self.read(HOME + '/registry.json'))
            need(set(state) == {'version', 'branches', 'baseline', 'initialized'} and state['version'] == 1,
                 'Invalid shared host registry')
            need(type(state['initialized']) is bool and isinstance(state['branches'], list) and isinstance(state['baseline'], dict),
                 'Invalid shared host registry')
            ids = set()
            for b in state['branches']:
                need(set(b) == FIELDS | {'legacy', 'status', 'files'} and type(b['legacy']) is bool
                     and b['status'] in ('pending', 'active'), 'Invalid branch registry entry')
                validate({k: b[k] for k in FIELDS})
                need(b['id'] not in ids, 'Duplicate registry branch'); ids.add(b['id'])
                need(isinstance(b['files'], dict), 'Invalid immutable file manifest')
                expected = set(self.file_hashes(b, b'', b''))
                need(set(b['files']) == expected and all(re.fullmatch('[0-9a-f]{64}', x) for x in b['files'].values()),
                     'Unexpected immutable file manifest')
            for b in state['branches']:
                collisions([x for x in state['branches'] if x['id'] != b['id']], {k: b[k] for k in FIELDS})
            need(not state['baseline'] or set(state['baseline']) == set(BASE_FILES), 'Unexpected firewall baseline')
            return state
        except (ValueError, TypeError, KeyError):
            raise Error('Invalid shared host registry') from None

    def save(self, state):
        self.atomic(HOME + '/registry.json', (json.dumps(state, sort_keys=True, separators=(',', ':')) + '\n').encode())

    def base_hashes(self):
        result = {}
        for name in BASE_FILES:
            path = self.path(name)
            need(path.is_file() and not path.is_symlink() and path.stat().st_uid == self.uid,
                 'Unsafe firewall base configuration')
            result[name] = digest(path.read_bytes())
        return result

    def claim_host(self, state):
        # Publish only a complete initial registry. An interruption between
        # mkdir(HOME) and save() must not strand an empty unresumable HOME.
        claim = Path(tempfile.mkdtemp(prefix='.spn-cascade-ru-claim-', dir=self.path('/opt')))
        try:
            (claim / 'branches').mkdir(mode=0o700)
            registry = claim / 'registry.json'
            fd = os.open(registry, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write((json.dumps(state, sort_keys=True, separators=(',', ':')) + '\n').encode())
                stream.flush(); os.fsync(stream.fileno())
            self.sync_directory(claim)
            # GNU mv -Tn does not replace even an existing empty target. The
            # per-host flock serializes managers; a foreign target is rejected.
            self.run('mv', '-Tn', '--', str(claim), str(self.path(HOME)))
            need(not claim.exists(), 'Shared host registry appeared concurrently')
            self.sync_directory(self.path('/opt'))
        finally:
            if claim.exists():
                (claim / 'registry.json').unlink(missing_ok=True)
                (claim / 'branches').rmdir()
                claim.rmdir()

    def audit_files(self, state):
        registered = {b['interface'] for b in state['branches']}
        wireguard = self.path('/etc/wireguard')
        if wireguard.exists() or wireguard.is_symlink():
            self.directory('/etc/wireguard')
            need({p.name for p in wireguard.iterdir()} <= {x + '.conf' for x in registered}, 'Unowned WireGuard files exist')
        for b in state['branches']:
            self.directory(branch_dir(b))
            for path, wanted in b['files'].items():
                if self.path(path).exists() or self.path(path).is_symlink():
                    need(digest(self.read(path)) == wanted, 'An existing branch configuration changed')
                else:
                    need(b['status'] == 'pending', 'An active branch configuration is missing')
            if b['status'] == 'active':
                validate_wg(self.read(branch_dir(b) + '/wg.conf'), b)
                need(normalize_proxy(self.read(branch_dir(b) + '/haproxy.cfg')) == proxy_text(b), 'Invalid existing branch proxy')

    def firewall(self, state, *, fresh=False):
        if fresh:
            status = self.run('ufw', 'status', optional=True)
            need(not status or 'Status: active' not in status, 'An unowned active firewall exists')
            text = self.run('ufw', 'show', 'added', optional=True)
            need(not text or not parse_rules(text), 'Unowned stored firewall rules exist')
            if status is not None:
                need(self.run('dpkg', '--verify', 'ufw') == '', 'Installed UFW package configuration changed')
            for command in (('nft', 'list', 'ruleset'), ('iptables-save',), ('ip6tables-save',)):
                text = self.run(*command, optional=True)
                if not text:
                    continue
                if command[0] == 'nft':
                    need(not text.strip(), 'Unowned nftables rules exist')
                else:
                    need(not any(line.startswith('-') or (line.startswith(':') and line.split()[1] != 'ACCEPT')
                                 for line in text.splitlines()), 'Unowned netfilter rules exist')
            return
        active = self.allowed_rules([b for b in state['branches'] if b['status'] == 'active'])
        text = self.run('ufw', 'show', 'added', optional=not active and not state['initialized'])
        if text is None:
            need(not active and not state['initialized'] and not state['baseline'], 'Owned firewall disappeared')
            return
        actual = parse_rules(text)
        permitted = self.allowed_rules(state['branches'])
        need(active <= actual <= permitted, 'Firewall has unknown rules or lost an active branch rule')
        if state['initialized'] or active:
            need('Status: active' in self.run('ufw', 'status'), 'Owned firewall is inactive')
            need('Default: deny (incoming), allow (outgoing),' in self.run('ufw', 'status', 'verbose'),
                 'Owned firewall defaults changed')
        for path, wanted in state['baseline'].items():
            # UFW packaged files use ordinary root-owned modes, not secret 600.
            file = self.path(path)
            need(file.is_file() and not file.is_symlink() and file.stat().st_uid == self.uid
                 and digest(file.read_bytes()) == wanted, 'Owned firewall base configuration changed')

    def allowed_rules(self, branches):
        result = rules(branches)
        if branches:
            result.update(('tcp', str(port), 'any', 'any', '') for port in self.monitoring_ports)
        return result

    def host(self, state, wanted, *, candidate=True):
        branches = state['branches']
        if candidate:
            need(not any(b['status'] == 'pending' and b['id'] != wanted['id'] for b in branches),
                 'Complete the previously interrupted branch before adding another')
            collisions(branches, wanted)
        else:
            need(all(b['ruIP'] == wanted['ruIP'] and b['sshPort'] == wanted['sshPort'] for b in branches),
                 'Shared host identity differs')
        self.phase = 'IPv4 address inventory'
        addresses = json.loads(self.run('ip', '-j', '-4', 'addr', 'show', 'up', 'scope', 'global'))
        count = sum(a.get('local') == wanted['ruIP'] for link in addresses for a in link.get('addr_info', []))
        need(count == 1, 'RU public IPv4 must be assigned once to an UP interface')
        self.phase = 'WireGuard interface inventory'
        try:
            # Some iproute2 builds emit [{},{}] for the filtered JSON query.
            # Inspect detailed *unfiltered* links and select info_kind instead;
            # never ignore malformed rows and accidentally hide a foreign WG.
            existing = set(host_checks.wireguard_interfaces(runner=self.run))
            self.phase = 'provider monitoring inventory'
            policy = host_checks.load_policy(self.path(self.stage + '/host-policy.json'), uid=self.uid)
            self.monitoring_ports = host_checks.verified_monitoring_ports(runner=self.run, root=self.root, uid=self.uid,
                                                                         keep_tcp_ports=policy['keepTcpPorts'])
        except host_checks.CheckError as error:
            raise Error(str(error)) from None
        owned = {b['interface'] for b in branches}
        active = {b['interface'] for b in branches if b['status'] == 'active'}
        need(active <= existing <= owned, 'Non-owned or missing active WireGuard interface')
        all_addresses = {b[k] for b in branches + [wanted] for k in ('localWG', 'peerWG')}
        self.phase = 'IPv4 route inventory'
        for route in json.loads(self.run('ip', '-j', '-4', 'route', 'show', 'table', 'all')):
            destination = route.get('dst', 'default')
            if destination in ('default', '0.0.0.0/0'):
                continue
            network = ipaddress.IPv4Network(destination, strict=False)
            if not any(ipaddress.IPv4Address(x) in network for x in all_addresses):
                continue
            matches = [b for b in branches if route.get('dev') == b['interface'] and
                       str(network) in (b['localWG'] + '/32', b['peerWG'] + '/32')]
            need(matches, 'A tunnel address overlaps a non-owned route or subnet')
        self.phase = 'TCP and UDP listener inventory'
        for row in self.run('ss', '-H', '-lnut').splitlines():
            fields = row.split()
            need(len(fields) >= 5, 'Cannot parse host socket inventory')
            protocol, endpoint = fields[0], fields[4]
            address, port = endpoint.rsplit(':', 1)
            address = address.strip('[]').split('%')[0]
            if address in ('::1',) or address.startswith('127.') or address.startswith('::ffff:127.'):
                continue
            if protocol == 'tcp' and port == str(wanted['sshPort']):
                continue
            if protocol == 'tcp' and port in {str(p) for p in self.monitoring_ports}:
                continue
            if protocol == 'udp' and port in ('68', '546'):
                continue
            allowed = any((protocol == 'tcp' and address == b['ruIP'] and port == str(b['entryPort'])) or
                          (protocol == 'udp' and port == str(b['ruWGPort'])) for b in branches)
            need(allowed, 'An unrelated or colliding public listener exists')
        for b in branches:
            if b['status'] == 'active':
                self.run('systemctl', 'is-active', '--quiet', 'wg-quick@' + b['interface'] + '.service')
                self.run('systemctl', 'is-active', '--quiet', unit_name(b))
        self.phase = 'managed file audit'
        self.audit_files(state)
        self.phase = 'firewall audit'
        self.firewall(state, fresh=not branches)
        if not branches:
            for path in ('/etc/haproxy', '/var/lib/docker', '/var/lib/containerd', '/etc/docker/daemon.json'):
                need(not self.path(path).exists() and not self.path(path).is_symlink(), 'Non-owned proxy/container state exists')
            need(self.run('docker', 'info', optional=True) is None, 'Unowned Docker daemon exists')

    def preflight(self, state):
        self.host(state, self.branch)
        self.immutable(self.stage + '/preflight-ok', (params(self.branch) + '|' + str(self.branch['entryPort'])
                                                     + '|' + str(self.branch['ruWGPort']) + '\n').encode())

    def staged(self):
        b = self.branch
        need(self.read(self.stage + '/.spn-deployment').decode().strip() == b['id'] + '|ru', 'Staged owner differs')
        for path in ('node.env', 'docker-compose.yml'):
            need(not self.path(self.stage + '/' + path).exists() and not self.path(self.stage + '/' + path).is_symlink(),
                 'EU-only configuration must not be staged on RU')
        wg = self.read(self.stage + '/wg.conf'); validate_wg(wg, b)
        proxy = self.read(self.stage + '/haproxy.cfg')
        need(normalize_proxy(proxy) == proxy_text(b), 'Unexpected RU proxy or fallback')
        return wg, proxy

    def packages(self):
        if self.path(HOME + '/packages-ok').exists():
            self.read(HOME + '/packages-ok')
            self.run('haproxy', '-v'); self.run('wg', '--version'); self.run('ufw', '--version')
            return
        # Legacy ownership proves these packages already exist; never touch its
        # package service or run APT while a registered branch is carrying traffic.
        if not self.path(HOME + '/package-mask').exists() and self.run('haproxy', '-v', optional=True) is not None:
            self.run('wg', '--version'); self.run('ufw', '--version')
        else:
            override = self.path('/run/systemd/system/haproxy.service')
            mask_owned = self.path(HOME + '/package-mask').exists()
            if not mask_owned:
                need(not override.exists() and not override.is_symlink(), 'Unowned HAProxy service override')
            else:
                need(self.read(HOME + '/package-mask') == b'haproxy.service\n', 'Package mask intent changed')
                need((not override.exists() and not override.is_symlink())
                     or (override.is_symlink() and os.readlink(override) == '/dev/null'),
                     'Unowned HAProxy service override')
            self.immutable(HOME + '/package-mask', b'haproxy.service\n')
            self.run('systemctl', 'mask', '--runtime', 'haproxy.service')
            for attempt in range(121):
                try:
                    self.run('apt-get', '-o', 'DPkg::Lock::Timeout=600', 'update')
                    break
                except Error:
                    # Check explicit APT lock errors without logging raw output.
                    if self.runner:
                        raise
                    process = subprocess.run(['apt-get', '-o', 'DPkg::Lock::Timeout=600', 'update'], capture_output=True, text=True)
                    if not process.returncode:
                        break
                    need(attempt < 120 and any(x in process.stderr for x in ('Could not get lock', 'Unable to acquire')),
                         'APT update failed without a recoverable lock error')
                    self.sleep(5)
            self.run('apt-get', '-o', 'DPkg::Lock::Timeout=600', 'install', '-y', 'ca-certificates', 'python3',
                     'wireguard-tools', 'ufw', 'haproxy')
            self.run('systemctl', 'unmask', '--runtime', 'haproxy.service')
            self.run('systemctl', 'disable', 'haproxy.service')
        self.immutable(HOME + '/packages-ok', b'owned\n')

    def install(self, state):
        self.host(state, self.branch)
        need(self.read(self.stage + '/preflight-ok').decode().strip() ==
             params(self.branch) + '|' + str(self.branch['entryPort']) + '|' + str(self.branch['ruWGPort']),
             'Preflight parameters differ')
        wg, proxy = self.staged()
        source = self.run('ip', '-4', 'route', 'get', self.branch['euIP']).split()
        need('src' in source and source[source.index('src') + 1] == self.branch['ruIP'], 'Preferred public source differs')
        if not self.path(HOME).exists():
            self.claim_host(state)
        existing = next((x for x in state['branches'] if x['id'] == self.branch['id']), None)
        if existing and existing['status'] == 'active':
            need(self.read(branch_dir(existing) + '/wg.conf') == wg and
                 self.read(branch_dir(existing) + '/haproxy.cfg') == proxy, 'Repeated staged configuration differs')
            return
        b = existing or dict(self.branch, legacy=False, status='pending')
        directory = branch_dir(b)
        if not existing:
            self.directory(directory, create=True)
            b['files'] = self.file_hashes(b, wg, proxy)
            state['branches'].append(b)
            # Persist intent BEFORE touching UFW/live files/units. Recovery only
            # accepts changes covered by this immutable intent.
            self.save(state)
        need(b['files'] == self.file_hashes(b, wg, proxy), 'Pending branch configuration changed')
        self.immutable(directory + '/wg.conf', wg); self.immutable(directory + '/haproxy.cfg', proxy)
        self.immutable(directory + '/haproxy.service', unit_text(b))
        if any(x['status'] == 'active' for x in state['branches']):
            # Active/legacy hosts must already have all shared packages.
            self.run('haproxy', '-v'); self.run('wg', '--version'); self.run('ufw', '--version')
            self.immutable(HOME + '/packages-ok', b'owned\n')
        else:
            self.packages()
        if not self.path('/etc/wireguard').exists():
            self.directory('/etc/wireguard', create=True)
        self.immutable('/etc/wireguard/' + b['interface'] + '.conf', wg)
        self.immutable('/etc/systemd/system/' + unit_name(b), unit_text(b))
        self.run('haproxy', '-c', '-f', directory + '/haproxy.cfg')
        before = parse_rules(self.run('ufw', 'show', 'added'))
        active = self.allowed_rules([x for x in state['branches'] if x['status'] == 'active'])
        need(active <= before <= self.allowed_rules(state['branches']), 'Unknown firewall rules appeared during add')
        commands = [(['allow', str(b['sshPort']) + '/tcp'], ('tcp', str(b['sshPort']), 'any', 'any', '')),
                    (['allow', str(b['entryPort']) + '/tcp'], ('tcp', str(b['entryPort']), 'any', 'any', '')),
                    (['allow', 'from', b['euIP'], 'to', 'any', 'port', str(b['ruWGPort']), 'proto', 'udp'],
                     ('udp', str(b['ruWGPort']), b['euIP'], 'any', ''))]
        commands += [(['allow', str(port) + '/tcp'], ('tcp', str(port), 'any', 'any', ''))
                     for port in self.monitoring_ports]
        for args, rule in commands:
            if rule not in before:
                self.run('ufw', *args, 'comment', 'SPN-CASCADE-' + b['id'][:12])
        if not state['initialized'] and not active:
            self.run('ufw', 'default', 'deny', 'incoming')
            self.run('ufw', 'default', 'allow', 'outgoing')
            self.run('ufw', '--force', 'enable')
        need(parse_rules(self.run('ufw', 'show', 'added')) == self.allowed_rules(state['branches']), 'Firewall union did not converge')
        if not state['baseline']:
            state['baseline'] = self.base_hashes()
        state['initialized'] = True
        self.save(state)
        self.run('systemctl', 'daemon-reload')
        self.run('systemctl', 'enable', '--now', 'wg-quick@' + b['interface'] + '.service')
        self.run('systemctl', 'enable', '--now', unit_name(b))
        b['status'] = 'active'
        self.save(state)
        self.host(state, self.branch)

    def verify(self, state):
        self.host(state, self.branch)
        b = next((x for x in state['branches'] if x['id'] == self.branch['id']), None)
        need(b and b['status'] == 'active', 'Branch installation is incomplete')
        wg, proxy = self.staged()
        need(self.read(branch_dir(b) + '/wg.conf') == wg and self.read(branch_dir(b) + '/haproxy.cfg') == proxy,
             'Staged branch configuration differs')
        i, peer = validate_wg(wg, b)
        interface = b['interface']
        def show(key):
            return self.run('wg', 'show', interface, key).strip()
        need(show('peers') == peer['PublicKey'] and show('listen-port') == str(b['ruWGPort']) and show('fwmark') == 'off',
             'WireGuard identity, UDP port or policy routing changed')
        need(show('public-key') == self.run('wg', 'pubkey', data=i['PrivateKey'] + '\n').strip(), 'WireGuard private identity changed')
        need(show('allowed-ips') == peer['PublicKey'] + '\t' + b['peerWG'] + '/32', 'WireGuard peer route changed')
        need(show('persistent-keepalive') == peer['PublicKey'] + '\t25', 'WireGuard keepalive changed')
        need(show('endpoints') == peer['PublicKey'] + '\t' + b['euIP'] + ':' + str(b['euWGPort']), 'WireGuard endpoint changed')
        need(show('preshared-keys') == peer['PublicKey'] + '\t' + peer['PresharedKey'], 'WireGuard preshared key changed')
        for _ in range(36):
            row = show('latest-handshakes').split()
            if len(row) == 2 and row[0] == peer['PublicKey'] and row[1].isdigit() and 0 <= time.time() - int(row[1]) <= 180:
                break
            self.sleep(1)
        else:
            raise Error('No fresh authenticated WireGuard handshake; install both roles and check UDP reachability')
        sockets = self.run('ss', '-H', '-lnt', 'sport = :' + str(b['entryPort'])).splitlines()
        need(len(sockets) == 1 and sockets[0].split()[3] == b['ruIP'] + ':' + str(b['entryPort']), 'Owned branch entry listener differs')
        self.run('python3', '-c', 'import socket,sys; s=socket.create_connection((sys.argv[1],2443),1); s.close()', b['peerWG'])

    def inventory(self, state):
        self.host(state, self.branch, candidate=False)
        return [{key: b[key] for key in ('id', 'entryPort', 'ruWGPort', 'ruIP', 'euIP', 'interface', 'localWG', 'peerWG')}
                for b in state['branches']]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    need(len(args) in (11, 13), 'Invalid shared RU invocation')
    action, role, public, peer, panel, ssh, deployment, interface, local, remote, udp = args[:11]
    need(role == 'ru' and action in ('identity', 'inventory', 'preflight', 'install', 'verify'), 'Invalid shared RU action')
    entry, ru_udp = args[11:] if len(args) == 13 else ('443', udp)
    b = validate({'id': deployment, 'ruIP': public, 'euIP': peer, 'panelIP': panel, 'sshPort': int(ssh),
                  'interface': interface, 'localWG': local, 'peerWG': remote, 'euWGPort': int(udp),
                  'entryPort': int(entry), 'ruWGPort': int(ru_udp)})
    need(os.geteuid() == 0, 'Root is required')
    manager = Manager(b)
    manager.directory(manager.stage)
    manager.phase = 'shared host lock'
    lockpath = Path('/run/lock/spn-cascade-ru.lock')
    fd = os.open(lockpath, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        s = os.fstat(lock.fileno())
        need(s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o600 and s.st_nlink == 1, 'Unsafe shared host lock')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        manager.phase = 'shared registry'
        state = manager.load()
        if action == 'identity':
            machine = Path('/etc/machine-id').read_text().strip()
            fingerprint = manager.run('ssh-keygen', '-lf', '/etc/ssh/ssh_host_ed25519_key.pub', '-E', 'sha256').split()[1]
            need(re.fullmatch('[0-9a-f]{32}', machine) and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint), 'Invalid machine identity')
            print(json.dumps({'machineId': machine, 'sshFingerprint': fingerprint,
                              'ownedInstallation': any(x['id'] == deployment for x in state['branches'])}))
        elif action == 'inventory':
            print(json.dumps(manager.inventory(state), separators=(',', ':')))
        else:
            getattr(manager, action)(state)
            print('Shared RU ' + action + ' OK')


def error_message(error):
    if isinstance(error, Error):
        return str(error)
    phase, line = 'bootstrap', 0
    frame = error.__traceback__
    while frame:
        owner = frame.tb_frame.f_locals.get('self', frame.tb_frame.f_locals.get('manager'))
        if isinstance(owner, Manager):
            phase = owner.phase
        if Path(frame.tb_frame.f_code.co_filename).name == 'shared_ru.py':
            line = frame.tb_lineno
        frame = frame.tb_next
    number = getattr(error, 'errno', None)
    extra = '; errno=' + str(number) if type(number) is int else ''
    # Exception messages/tracebacks may contain private inputs. Only controlled
    # phase labels, class, errno and this source line are safe to display.
    return f'Shared RU failed during {phase}: {type(error).__name__}{extra}; shared_ru.py:{line}'


if __name__ == '__main__':
    try:
        main()
    except (Error, OSError, ValueError, KeyError) as error:
        # Never echo exception details from parsers, commands, keys or paths.
        print('Stopped: ' + error_message(error), file=sys.stderr)
        raise SystemExit(1)
