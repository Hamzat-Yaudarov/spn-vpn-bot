"""REALITY guest effects, deliberately separated from paid provider effects.

Bootstrap never changes an existing DNS record or enables a published host.
Only cutover can do that, after fresh independent RF/outside VPN evidence.
Private append-only journals make idempotent guest effects resumable without
replaying an ambiguous DNS PATCH or creating a second panel identity.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from .credentials import private_directory
from .policy import classify_health
from .probe import ProbeRunner

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('voxa_fleet_reality_replacement', _ROOT / 'ops/universal-reality/replace.py')
r = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r)
d, u = r.d, r.d.u


class ExecutorError(RuntimeError):
    """Static safe errors only; no credential or subprocess diagnostics."""


def _need(condition, reason='Guest identity or executor configuration is not confirmed'):
    if not condition:
        raise ExecutorError(reason)


def _id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


class FleetSSH(d.SSH):
    """Pinned, key-only, noninteractive SSH; no control-plane environment."""

    def __init__(self, plan, known_hosts=None, *, identity_file):
        _need(known_hosts is not None, 'An enrolled, pinned SSH host key is required')
        super().__init__(plan, known_hosts)
        self.common += ['-i', str(identity_file), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                        '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
                        '-o', 'PubkeyAuthentication=yes', '-o', 'RequestTTY=no', '-o', 'ForwardAgent=no',
                        '-o', 'ClearAllForwardings=yes']
        self.environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(Path.home()), 'LANG': 'C.UTF-8'}


class GuestExecutor:
    def __init__(self, store, credentials, data_dir, runtime_config, enrollment, probe_runner=None):
        self.store, self.credentials = store, credentials
        self.data_dir = Path(data_dir).expanduser().absolute()
        self.config, self.enrollment = dict(runtime_config or {}), enrollment
        self.probe_runner = probe_runner
        if self.probe_runner is None and self.config.get('engine') and self.config.get('engine_sha256'):
            self.probe_runner = ProbeRunner(self.config['engine'], self.config['engine_sha256'],
                                            interface=self.config.get('interface'),
                                            outside_command=self.config.get('outside_command'))

    def _credentials(self):
        value = self.credentials.read() if callable(getattr(self.credentials, 'read', None)) else self.credentials
        _need(isinstance(value, dict), 'Private executor credentials are unavailable')
        return value

    def _key(self):
        path = self.config.get('ssh_private_key') or getattr(self.enrollment, 'ssh_private_key', None)
        _need(isinstance(path, (str, Path)), 'Enrollment SSH private key is not configured')
        path = Path(path).expanduser().absolute()
        _need(not any(p.is_symlink() for p in (path, *path.parents)), 'Unsafe SSH private key path')
        try:
            info = path.stat()
        except OSError:
            raise ExecutorError('Enrollment SSH private key is unavailable') from None
        _need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
              and info.st_nlink == 1 and 0 < info.st_size < 16384, 'SSH private key must be owned and mode 0600')
        return path

    def ready(self):
        reasons = []
        try:
            creds = self._credentials()
            for key in ('remnawave_url', 'remnawave_token', 'cloudflare_token', 'probe_subscription_url'):
                if not isinstance(creds.get(key), str) or not creds[key]:
                    reasons.append('missing_' + key)
            if creds.get('remnawave_url'):
                u.panel_url(creds['remnawave_url'])
            subscription = creds.get('probe_subscription_url', '')
            parsed = urlsplit(subscription)
            _need(parsed.scheme == 'https' and parsed.hostname == 'sub.wayspn.online'
                  and parsed.port in (None, 443) and parsed.path not in ('', '/')
                  and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                  and not any(c in subscription for c in '\r\n"\\'),
                  'Private probe subscription URL is invalid')
            self._key()
        except Exception:
            reasons.append('private_credentials_or_ssh_key_invalid')
        if not _id(self.config.get('os_image_id')):
            reasons.append('ubuntu_24_04_image_not_configured')
        if not isinstance(self.config.get('interface'), str) or not self.config['interface']:
            reasons.append('physical_rf_interface_not_configured')
        try:
            d.probe.validate_interface(self.config.get('interface'))
            _need(5 <= self.config.get('wait', 120) <= 600
                  and type(self.config.get('ssh_port', 22)) is int
                  and 1 <= self.config.get('ssh_port', 22) <= 65535
                  and self.config.get('ssh_port', 22) not in (443, 2222))
            d.dns.normalize_name(self.config.get('dns_zone', d.DEFAULT_DNS_ZONE))
        except Exception:
            reasons.append('guest_runtime_config_invalid')
        try:
            result = self.enrollment.ready()
            _need(result is True or isinstance(result, dict) and result.get('ready') is True)
        except Exception:
            reasons.append('enrollment_not_ready')
        try:
            _need(self.probe_runner is not None and self.probe_runner.readiness().get('configured') is True)
        except Exception:
            reasons.append('independent_vpn_probes_not_ready')
        return {'ready': not reasons, 'reasons': list(dict.fromkeys(reasons))}

    def prepare_order(self, server, op):
        _need(self.ready()['ready'], 'Guest executor must be ready before a paid order')
        _need(op.get('server_id') == server.get('id') and _id(op.get('id')), 'Order identity does not match server')
        options = self.enrollment.prepare(op['id'])
        _need(isinstance(options, dict) and isinstance(options.get('ssh_public_key'), str)
              and options['ssh_public_key'].startswith('ssh-ed25519 ') and isinstance(options.get('user_data'), str)
              and options['user_data'] and len(options['user_data']) <= 65536, 'Enrollment options are incomplete')
        return {'os_image_id': self.config['os_image_id'], 'ssh_public_key': options['ssh_public_key'],
                'user_data': options['user_data']}

    def _paths(self, server):
        private_directory(self.data_dir)
        roots = [private_directory(self.data_dir / name) for name in ('installations', 'executor', 'runtime-private')]
        key = u.digest(server['id'])[:24]
        return roots[0] / key, private_directory(roots[1] / key)

    def _original(self, server):
        installation, _ = self._paths(server)
        path = Path(server['original_state_path']).expanduser().absolute() if server.get('original_state_path') else installation
        if server.get('original_state_path'):
            _need(path.is_dir(), 'Registered original installation state is missing')
        return u.Store(path)

    def _journal(self, server, op, generation):
        _need(generation.get('server_id') == server.get('id') and _id(generation.get('operation_id'))
              and _id(generation.get('vps_id')), 'Exact owned candidate generation is required')
        owner = op.get('data', {}).get('original_operation_id', op.get('id'))
        _need(owner == generation['operation_id'] and op.get('server_id') == server['id'], 'Candidate operation identity differs')
        _, root = self._paths(server)
        journal = u.Store(root / generation['operation_id'])
        for directory in root.iterdir():
            _need(directory.is_dir() and not directory.is_symlink(), 'Unsafe executor operation directory')
            other = u.Store(directory)
            if directory != journal.path and other.load('intent.json'):
                _need(other.load('commit.json') is not None or other.load('cancelled.json') is not None,
                      'Another guest operation is still pending for this domain')
        saved = journal.load('intent.json')
        if saved:
            _need(saved.get('server_id') == server['id'] and saved.get('operation_id') == owner
                  and saved.get('vps_id') == generation['vps_id'] and saved.get('domain') == server['domain']
                  and saved.get('toIp') == d.public_ipv4(generation['ip']), 'Saved candidate identity changed')
        return journal

    def _api(self):
        creds = self._credentials()
        return d.API(u.panel_url(creds['remnawave_url']), creds['remnawave_token'])

    def _plan(self, api, original, server, generation):
        plan = original.load('plan.json')
        base = u.panel_url(self._credentials()['remnawave_url'])
        owner = original.load('fleet.owner.json')
        if owner is not None:
            _need(owner == {'version': 1, 'server_id': server['id'], 'domain': server['domain'], 'panel': base},
                  'Original installation belongs to another logical fleet server')
        if plan is None:
            _need(not server.get('original_state_path') and not server.get('current_ip'), 'Existing logical server has no original plan')
            squads = server.get('metadata', {}).get('squads')
            _need(isinstance(squads, list) and squads and all(_id(s) for s in squads), 'Explicit initial squad UUIDs are required')
            argv = ['--ip', generation['ip'], '--domain', server['domain'], '--name', server['name'],
                    '--ssh-port', str(self.config.get('ssh_port', 22)),
                    '--dns-zone', self.config.get('dns_zone', d.DEFAULT_DNS_ZONE)]
            for squad in squads:
                argv.extend(['--squad', squad])
            if server.get('country'):
                argv.extend(['--country', server['country']])
            plan = d.prepare(api, original, d.parser().parse_args(argv), base, d.panel_ipv4(base))
        _need(plan.get('domain') == server['domain'] and plan.get('panel') == base
              and plan.get('mode') in (None, 'direct') and plan.get('transport') == 'raw-reality-vision-safari-v1'
              and plan.get('dns', {}).get('provider') == 'cloudflare' and plan['dns'].get('ttl') == 60,
              'Only the registered direct DNS-backed REALITY installation can be used')
        if owner is None and server.get('original_state_path'):
            # Imported legacy installs need their original completed ownership
            # receipts. First Fleet retries have fleet.owner, even preactivation.
            r.validate_original(original, server['domain'], base)
        original.save('fleet.owner.json', {'version': 1, 'server_id': server['id'], 'domain': server['domain'], 'panel': base})
        if (original.path / 'replacements').exists():
            _, pending = r.chain(original, plan)
            _need(pending is None, 'A manual replacement is pending for this original installation')
        return plan

    def _registration(self, original, plan):
        node, host, profile = (original.load(name + '.receipt.json') for name in ('node', 'tcp', 'profile'))
        binding = original.load('node.intent.json')['configProfile']
        return {'original_state_path': str(original.path), 'metadata': {
            'node_id': node['uuid'], 'host_id': host['uuid'], 'profile_id': profile['uuid'],
            'inbound_id': binding['activeInbounds'][0], 'cloudflare_record_id': original.load('dns.receipt.json')['id'],
            'installation_code': plan['code']}}

    def _validate_refs(self, server, original, plan):
        refs = self._registration(original, plan)['metadata']
        if server.get('original_state_path'):
            _need(all(server.get('metadata', {}).get(k) == v for k, v in refs.items()),
                  'Registered UUIDs or original DNS record ID changed')
        keys, secret = original.load('reality.private.json'), original.load('node-secret.json')
        _need(isinstance(keys, dict) and set(keys) == {'private', 'public', 'short'}
              and all(isinstance(keys.get(k), str) for k in keys)
              and re.fullmatch('[A-Za-z0-9_-]{43}', keys['private'])
              and re.fullmatch('[A-Za-z0-9_-]{43}', keys['public'])
              and re.fullmatch('[0-9a-f]{16}', keys['short']) and isinstance(secret, dict)
              and isinstance(secret.get('value'), str) and len(secret['value']) >= 20,
              'Original keys are missing; replacements never generate keys')
        return refs

    def _audit(self, api, original, plan, journal, *, no_snapshot=False):
        node = u.one(api.rows('nodes'), original.load('node.receipt.json')['uuid'])
        profile = u.one(api.rows('config-profiles'), original.load('profile.receipt.json')['uuid'])
        host = u.one(api.rows('hosts'), original.load('tcp.receipt.json')['uuid'])
        d.direct_controller(node)
        keys = original.load('reality.private.json')
        _need(profile.get('config') == d.profile_config(plan['code'], keys, plan.get('sni', d.SNI)),
              'Owned profile technical configuration or REALITY keys drifted')
        inbounds = profile.get('inbounds')
        _need(isinstance(inbounds, list) and len(inbounds) == 1 and inbounds[0].get('tag') == 'reality-' + plan['code'])
        inbound = inbounds[0]['uuid']
        binding = {'activeConfigProfileUuid': profile['uuid'], 'activeInbounds': [inbound]}
        _need(original.load('node.intent.json').get('configProfile') == binding
              and node.get('port') == 2222 and u.same(node, {'configProfile': binding}), 'Owned inbound or node binding drifted')
        nodes = api.rows('nodes')
        _need(all(row['uuid'] == node['uuid'] for row in nodes
                  if (row.get('configProfile') or {}).get('activeConfigProfileUuid') == profile['uuid']),
              'Owned profile is shared with a different node')
        initial_host = original.load('tcp.intent.json')
        technical = ('address', 'port', 'nodes', 'inbound', 'sni', 'alpn', 'fingerprint', 'securityLayer',
                     'mihomoX25519', 'overrideSniFromAddress', 'keepSniBlank')
        _need(initial_host.get('address') == plan['domain'] and initial_host.get('port') == 443
              and initial_host.get('nodes') == [node['uuid']]
              and initial_host.get('inbound') == {'configProfileUuid': profile['uuid'], 'configProfileInboundUuid': inbound}
              and initial_host.get('fingerprint') == d.FINGERPRINT and initial_host.get('sni') == plan.get('sni', d.SNI)
              and u.same(host, {k: initial_host[k] for k in technical if k in initial_host}),
              'Owned host technical configuration drifted')
        audience = sorted(row['uuid'] for row in api.rows('internal-squads') if inbound in u.ids(row['inbounds']))
        _need(set(plan['squads']) <= set(audience), 'Owned inbound disappeared from its required audience')
        current = {'node': {**d.node_snapshot(node), 'isDisabled': False}, 'host': r._host_snapshot(host),
                   'profile': {'uuid': profile['uuid'], 'name': profile.get('name'), 'configHash': u.digest(profile['config'])},
                   'inbound': inbound, 'squads': audience}
        intent, saved = journal.load('intent.json'), None if no_snapshot else journal.load('topology.json')
        _need(intent is not None)
        _need(not any(n['uuid'] != node['uuid'] and n.get('address') in (plan['domain'], intent['toIp']) for n in nodes),
              'Candidate IPv4 or endpoint domain is owned by another node')
        addresses, disabled = {plan['domain']}, False
        if journal.load('node-new.intent.json'):
            addresses.add(intent['toIp'])
            disabled = not journal.load('node-new.receipt.json')
        if journal.load('node-new.receipt.json') and not journal.load('node-domain.intent.json'):
            addresses = {intent['toIp']}
        if journal.load('node-domain.intent.json'):
            addresses = {plan['domain'], intent['toIp']}
            disabled = disabled or not journal.load('node-domain.receipt.json')
        if journal.load('node-domain.receipt.json'):
            addresses = {plan['domain']}
        if saved:
            _need(r._node_matches(node, saved['node']) and current['profile'] == saved['profile']
                  and current['inbound'] == saved['inbound'] and current['squads'] == saved['squads'],
                  'Owned identities, names, metadata or audience changed during the operation')
            expected_host = copy.deepcopy(saved['host'])
            if journal.load('host-publish.intent.json'):
                if journal.load('host-publish.receipt.json'):
                    _need(host.get('isDisabled') is False, 'Published host was disabled after this operation confirmed it')
                else:
                    _need(host.get('isDisabled') in (saved['host'].get('isDisabled'), False))
                expected_host['isDisabled'] = host['isDisabled']
            _need(current['host'] == expected_host, 'Host metadata or availability changed outside this operation')
        _need(node.get('address') in addresses and (node.get('isDisabled') is False or disabled),
              'Node routing or disabled state is not authorized by this operation')
        if original.load('activation.verified.json') and not saved:
            _need(host.get('isDisabled') is False, 'An intentionally disabled published host is never enabled automatically')
        return current

    def _dns(self, original, plan):
        client = d.dns.CloudflareDNS(self._credentials()['cloudflare_token'])
        zone = client.find_zone(plan['dns']['zone'])
        receipt = original.load('dns.receipt.json')
        if receipt:
            _need(zone.get('id') == receipt.get('zone_id') and receipt.get('name') == plan['domain'],
                  'Original Cloudflare record or zone identity changed')
        return client, zone

    def _intent(self, journal, original, plan, server, op, generation):
        saved = journal.load('intent.json')
        if saved is None:
            receipt = original.load('dns.receipt.json')
            current = server.get('current_ip') or (receipt or {}).get('content') or plan['ip']
            saved = {'version': 1, 'server_id': server['id'], 'operation_id': generation['operation_id'],
                     'vps_id': generation['vps_id'], 'domain': plan['domain'], 'planHash': u.digest(plan),
                     'fromIp': d.public_ipv4(current), 'toIp': d.public_ipv4(generation['ip']),
                     'initial': original.load('activation.verified.json') is None,
                     'original_state_path': str(original.path)}
            journal.save('intent.json', saved)
        _need(saved.get('planHash') == u.digest(plan) and saved.get('original_state_path') == str(original.path),
              'Original installation changed after this operation started')
        _need(journal.load('cancelled.json') is None, 'This rejected candidate journal is closed permanently')
        return saved

    def _runtime(self, api, original, plan, journal, generation):
        value = journal.load('runtime.json')
        if value is None:
            value = {**plan, 'ip': d.public_ipv4(generation['ip']), 'panelIp': d.panel_ipv4(plan['panel']),
                     'sshPort': self.config.get('ssh_port', plan['sshPort'])}
            journal.save('runtime.json', value)
        _need(value.get('ip') == generation['ip'] and value.get('id') == plan['id'], 'Candidate runtime identity changed')
        return value

    def _ssh(self, runtime, known_hosts):
        return FleetSSH(runtime, known_hosts, identity_file=self._key())

    def _enrolled(self, journal, generation):
        evidence = self.enrollment.verify(generation['operation_id'], generation['ip'])
        _need(isinstance(evidence, dict) and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', evidence.get('host_key_sha256', ''))
              and re.fullmatch('[0-9a-f]{32}', evidence.get('machine_id', '')), 'Enrollment machine/host key is not confirmed')
        if 'source_ip' in evidence:
            _need(evidence['source_ip'] == generation['ip'], 'Enrollment source differs from the candidate IPv4')
        if 'assigned_ips' in evidence:
            _need(generation['ip'] in evidence['assigned_ips'], 'Enrollment did not confirm the assigned IPv4')
        journal.save('enrollment.verified.json', evidence)
        known = d.trust.resolve(generation['ip'], journal.load('runtime.json')['sshPort'], journal,
                                expected=evidence['host_key_sha256'])
        return evidence, known

    @staticmethod
    def _identity(ssh, evidence):
        identity = json.loads(ssh.action('identity', capture=True))
        _need(identity.get('machineId') == evidence['machine_id']
              and identity.get('sshFingerprint') == evidence['host_key_sha256']
              and type(identity.get('ownedInstallation')) is bool, 'SSH guest identity differs from enrollment')
        return identity

    def bootstrap(self, server, op, generation):
        _need(self.ready()['ready'], 'Guest executor is not ready')
        original, journal, api = self._original(server), self._journal(server, op, generation), self._api()
        with original.lock():
            plan = self._plan(api, original, server, generation)
            intent = self._intent(journal, original, plan, server, op, generation)
            if all(original.load(k + '.receipt.json') for k in ('profile', 'node', 'tcp')):
                # Replacement drift/collisions must fail before even an owned
                # guest source-route change, not merely before panel routing.
                self._validate_refs(server, original, plan)
                snapshot = self._audit(api, original, plan, journal)
                if journal.load('topology.json') is None:
                    journal.save('topology.json', snapshot)
            client, zone = self._dns(original, plan)
            receipt = original.load('dns.receipt.json')
            if receipt is None:
                _need(intent['initial'], 'Existing guest has no original DNS ownership receipt')
                client.check_record(original, zone_id=zone['id'], name=plan['domain'], ipv4=plan['ip'], ttl=60)
                client.ensure_record(original, zone_id=zone['id'], name=plan['domain'], ipv4=plan['ip'], ttl=60)
            elif intent['fromIp'] != intent['toIp']:
                client.check_update(journal, receipt=receipt, old_ipv4=intent['fromIp'], new_ipv4=intent['toIp'])
            else:
                client.check_record(original, zone_id=zone['id'], name=plan['domain'], ipv4=plan['ip'], ttl=60)
            runtime = self._runtime(api, original, plan, journal, generation)
            enrolled, known = self._enrolled(journal, generation)
            with self._ssh(runtime, known) as ssh:
                ssh.upload({'managed-node.sh': (d.HERE / 'managed-node.sh').read_bytes(),
                            'preferred_source.py': (d.HERE / 'preferred_source.py').read_bytes()})
                ssh.action('preflight')
                self._identity(ssh, enrolled)
                ssh.source('check', capture=True)
                ssh.source('apply')
                _need(runtime['ip'] in ssh.assigned_ipv4(), 'Candidate IPv4 is not assigned to the enrolled guest')
                deployment = d.Deployment(api, original, plan)
                if not all(original.load(k + '.receipt.json') for k in ('profile', 'node')):
                    _need(intent['initial'], 'Existing profile/node ownership receipts are missing')
                    profile, node, inbound = deployment.provision()
                else:
                    profile = u.one(api.rows('config-profiles'), original.load('profile.receipt.json')['uuid'])
                    node = u.one(api.rows('nodes'), original.load('node.receipt.json')['uuid'])
                    inbound = original.load('node.intent.json')['configProfile']['activeInbounds'][0]
                secret = original.load('node-secret.json')
                if secret is None:
                    _need(intent['initial'], 'Existing node service secret is missing; it will not be rotated')
                    secret = {'value': api('GET', '/keygen')['secretKey']}
                    original.save('node-secret.json', secret)
                if not original.load('tcp.receipt.json'):
                    _need(intent['initial'], 'Existing host ownership receipt is missing')
                    deployment.host(profile, node, inbound)
                if intent['initial']:
                    deployment.attach(inbound)
                self._validate_refs(server, original, plan)
                snapshot = self._audit(api, original, plan, journal)
                if journal.load('topology.json') is None:
                    journal.save('topology.json', snapshot)
                route_intent = {**intent, 'snapshot': journal.load('topology.json')}
                ssh.upload(d.render_files(runtime, secret['value']))
                ssh.action('install')
                self._identity(ssh, enrolled)
                node = r.route_node(api, journal, route_intent, 'new', runtime['ip'])
                d.u.Deployment(api, journal, runtime, {}).wait_ready(node, self.config.get('wait', 120))
                ssh.action('verify')
                ssh.source('verify')
                self._audit(api, original, plan, journal)
            result = {'ok': True, 'ids_preserved': True}
            if not server.get('original_state_path'):
                result['registration'] = self._registration(original, plan)
            # Controller adoption can commit before its bootstrap_done flag.
            # Keep the originally returned registration on that exact replay.
            previous = journal.load('bootstrap.receipt.json')
            if previous is not None:
                _need(previous.get('ok') is True and previous.get('ids_preserved') is True)
                if previous.get('registration') is not None:
                    _need(previous['registration'] == self._registration(original, plan))
                result = previous
            journal.save('bootstrap.receipt.json', result)
            return result

    def _subscription(self):
        value = self._credentials().get('probe_subscription_url')
        _need(isinstance(value, str) and value and len(value) < 4096 and not any(c in value for c in '\r\n'))
        private_directory(self.data_dir)
        parent = private_directory(self.data_dir / 'runtime-private')
        path = parent / 'subscription.txt'
        if path.exists() or path.is_symlink():
            d.probe.private_file(path)
        fd, temporary = tempfile.mkstemp(prefix='.subscription-', dir=parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write((value + '\n').encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
        d.probe.subscription_url(path)
        return path

    def _round(self, server, ip, original, plan):
        _need(self.probe_runner is not None and self.probe_runner.readiness().get('configured') is True,
              'Independent VPN probe prerequisites are unavailable')
        subscription = self._subscription()
        documents = d.probe.fetch_documents(subscription, self.config['interface'])
        user = d.probe.credential(documents)
        keys = original.load('reality.private.json')
        leaf = d.probe.client_config(ip, keys['public'], keys['short'], user, 19080,
                                     self.config['interface'], sni=plan.get('sni', d.SNI), route='system')['outbounds'][0]
        return self.probe_runner.check(server, ip, outbound=leaf, expected_exit_ip=ip)

    def probe(self, server, generation):
        original = self._original(server)
        _, root = self._paths(server)
        journal = u.Store(root / generation['operation_id'])
        with original.lock():
            plan = original.load('plan.json')
            _need(journal.load('bootstrap.receipt.json') is not None and journal.load('cancelled.json') is None,
                  'Candidate bootstrap is not complete')
            self._validate_refs(server, original, plan)
            self._audit(self._api(), original, plan, journal)
            result = self._round(server, d.public_ipv4(generation['ip']), original, plan)
            journal.save('probe-' + str(time.time_ns()) + '.json', result)
            return result

    def _converge_dns(self, journal, runtime, zone):
        observed = journal.load('dns-cutover.observed.json')
        if observed is None:
            observed = {'domain': runtime['domain'], 'ip': runtime['ip'], 'observedAt': time.time()}
            journal.save('dns-cutover.observed.json', observed)
        _need(observed.get('domain') == runtime['domain'] and observed.get('ip') == runtime['ip'])
        # Even current public recursive answers cannot expire the panel's cache.
        r._wait_dns(runtime, zone, self.config.get('wait', 120),
                    not_before=observed['observedAt'] + runtime['dns']['ttl'])
        journal.save('dns.verified.json', {'domain': runtime['domain'], 'ip': runtime['ip'], 'recordPreserved': True})

    def cutover(self, server, op, generation):
        original, journal, api = self._original(server), self._journal(server, op, generation), self._api()
        with original.lock():
            plan = self._plan(api, original, server, generation)
            intent = self._intent(journal, original, plan, server, op, generation)
            _need(journal.load('bootstrap.receipt.json') is not None, 'Cannot cut over an unbootstrapped candidate')
            self._validate_refs(server, original, plan)
            self._audit(api, original, plan, journal)
            fresh = self._round(server, intent['toIp'], original, plan)
            journal.save('cutover-probe-' + str(time.time_ns()) + '.json', fresh)
            _need(classify_health(fresh, max_age_seconds=self.store.get_settings()['probe_max_age'])['verdict'] == 'healthy',
                  'Fresh independent RF and outside VPN proof is required before DNS or publication')
            client, zone = self._dns(original, plan)
            receipt = original.load('dns.receipt.json')
            if intent['fromIp'] != intent['toIp']:
                client.update_record(journal, receipt=receipt, old_ipv4=intent['fromIp'], new_ipv4=intent['toIp'])
            else:
                client.check_record(original, zone_id=zone['id'], name=plan['domain'], ipv4=plan['ip'], ttl=60)
            runtime = journal.load('runtime.json')
            self._converge_dns(journal, runtime, zone)
            node = r.route_node(api, journal, {**intent, 'snapshot': journal.load('topology.json')}, 'domain', plan['domain'])
            d.u.Deployment(api, journal, runtime, {}).wait_ready(node, self.config.get('wait', 120))
            host = u.one(api.rows('hosts'), original.load('tcp.receipt.json')['uuid'])
            if journal.load('topology.json')['host']['isDisabled']:
                _need(intent['initial'], 'Existing disabled host must not be auto-published')
                marker = {'uuid': host['uuid'], 'intentHash': u.digest(intent)}
                journal.save('host-publish.intent.json', marker)
                if host.get('isDisabled') is True:
                    api('PATCH', '/hosts', {'uuid': host['uuid'], 'isDisabled': False})
                _need(u.one(api.rows('hosts'), host['uuid']).get('isDisabled') is False, 'Initial host publication is not confirmed')
                journal.save('host-publish.receipt.json', marker)
            self._audit(api, original, plan, journal)
            result = {'ok': True, 'ids_preserved': True, 'dns_record_preserved': True}
            journal.save('cutover.receipt.json', result)
            return result

    def verify(self, server, op, generation):
        original, journal, api = self._original(server), self._journal(server, op, generation), self._api()
        with original.lock():
            plan = original.load('plan.json')
            _need(journal.load('cutover.receipt.json') is not None and journal.load('cancelled.json') is None,
                  'A completed cutover is required for verification')
            self._validate_refs(server, original, plan)
            self._audit(api, original, plan, journal)
            runtime = journal.load('runtime.json')
            _, zone = self._dns(original, plan)
            self._converge_dns(journal, runtime, zone)
            enrolled, known = self._enrolled(journal, generation)
            with self._ssh(runtime, known) as ssh:
                identity = self._identity(ssh, enrolled)
                _need(identity['ownedInstallation'] is True, 'Enrolled guest no longer has the owned installation')
                ssh.action('verify')
                ssh.source('verify')
            round_ = self._round(server, runtime['ip'], original, plan)
            journal.save('verify-probe-' + str(time.time_ns()) + '.json', round_)
            healthy = classify_health(round_, max_age_seconds=self.store.get_settings()['probe_max_age'])['verdict'] == 'healthy'
            if not healthy:
                return {'ok': False, 'ids_preserved': True, 'round': round_, 'dns_converged': True}
            keys = original.load('reality.private.json')
            exported = d.probe.verify_export(runtime['ip'], keys['public'], keys['short'], self._subscription(),
                                             interface=self.config['interface'], route='system',
                                             endpoint_address=plan['domain'], sni=plan.get('sni', d.SNI))
            _need(exported.get('passed') is True, 'Fresh subscription export does not preserve the endpoint domain and keys')
            journal.save('export-' + str(time.time_ns()) + '.json', exported)
            original.save('activation.verified.json', {'host': original.load('tcp.receipt.json')['uuid'], 'code': plan['code'],
                                                        'trafficAndExportVerified': True})
            journal.save('commit.json', {'version': 1, 'intentHash': u.digest(journal.load('intent.json')),
                                         'domain': plan['domain'], 'ip': runtime['ip'], 'ids_preserved': True,
                                         'dns_record_preserved': True, 'independentTrafficAndExportVerified': True})
            return {'ok': True, 'ids_preserved': True, 'round': round_, 'dns_converged': True}

    def observe(self, server):
        original, api = self._original(server), self._api()
        with original.lock():
            plan = original.load('plan.json')
            _need(plan is not None and plan.get('domain') == server['domain'] and server.get('current_ip'),
                  'A committed logical guest is required before monitoring')
            self._validate_refs(server, original, plan)
            class ReadOnlyAudit:
                @staticmethod
                def load(name):
                    return {'toIp': server['current_ip']} if name == 'intent.json' else None
            # Do not turn a foreign panel edit or disabled endpoint into a
            # purchasable RF incident. Monitoring pins technical identity, not
            # an old display-name snapshot from an earlier generation.
            self._audit(api, original, plan, ReadOnlyAudit(), no_snapshot=True)
            return self._round(server, d.public_ipv4(server['current_ip']), original, plan)

    def restore(self, server, op, generation):
        original, journal, api = self._original(server), self._journal(server, op, generation), self._api()
        with original.lock():
            plan = original.load('plan.json')
            intent = journal.load('intent.json')
            # A provisioning failure can precede any guest effect/journal.
            if intent is None:
                _need(journal.load('runtime.json') is None, 'Partial guest state has no ownership intent')
                result = {'ok': True, 'ids_preserved': True}
                journal.save('cancelled.json', {'version': 1, 'vps_id': generation['vps_id'], 'reason': 'no_guest_effects'})
                journal.save('restore.receipt.json', result)
                return result
            _need(journal.load(d.dns.UPDATE_PENDING) is None and journal.load(d.dns.UPDATE_RECEIPT) is None
                  and journal.load('dns-cutover.observed.json') is None and journal.load('host-publish.intent.json') is None
                  and journal.load('commit.json') is None,
                  'Candidate may serve published DNS; automatic cleanup is forbidden after cutover')
            receipt = original.load('dns.receipt.json')
            _need(receipt is not None, 'Original DNS ownership was not recorded; cleanup needs manual inspection')
            client, zone = self._dns(original, plan)
            if intent['fromIp'] != intent['toIp']:
                client.check_update(journal, receipt=receipt, old_ipv4=intent['fromIp'], new_ipv4=intent['toIp'])
            else:
                client.check_record(original, zone_id=zone['id'], name=plan['domain'], ipv4=plan['ip'], ttl=60)
            if journal.load('node-new.intent.json'):
                self._validate_refs(server, original, plan)
                self._audit(api, original, plan, journal)
                snapshot = journal.load('topology.json')
                _need(snapshot is not None, 'Node route journal has no owned topology snapshot')
                r.route_node(api, journal, {**intent, 'snapshot': snapshot}, 'domain', plan['domain'])
                self._audit(api, original, plan, journal)
            result = {'ok': True, 'ids_preserved': True}
            journal.save('cancelled.json', {'version': 1, 'vps_id': generation['vps_id'], 'intentHash': u.digest(intent),
                                            'reason': 'candidate_rejected_before_dns_cutover'})
            journal.save('restore.receipt.json', result)
            return result

    def recover(self, stage, server, op, generation):
        _need(stage in ('bootstrap', 'cutover', 'restore'), 'Unknown executor recovery stage')
        # Re-enter the exact same scoped phase: SSH installation is owned and
        # idempotent; panel ensures reconcile lost POST by the immutable body;
        # DNS helpers refuse to repeat an ambiguous PATCH. No financial calls.
        journal = self._journal(server, op, generation)
        saved = journal.load(stage + '.receipt.json')
        result = getattr(self, stage)(server, op, generation)
        if saved:
            _need(result == saved, 'Recovered stage evidence differs from the original receipt')
            return saved
        return result
