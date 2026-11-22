"""Offline cascade configuration tests: no SSH, DNS, panel, or network writes."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


HERE = Path(__file__).resolve().parent


def local_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


c = local_module('cascade_config_under_test', HERE / 'config.py')
u = local_module('cascade_config_store_under_test', HERE.parent / 'universal-direct/deploy.py')


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = u.Store(self.root / 'private-state')
        self.plan = {
            'format': 1, 'id': 'a' * 32, 'code': 'a1b2c3',
            'ru': {'ip': '8.8.4.4', 'sshPort': 22}, 'eu': {'ip': '9.9.9.9', 'sshPort': 22},
            'panel': 'https://panel.example.com/api', 'panelIp': '1.1.1.1',
            'name': 'EU via RU', 'country': 'FI', 'domain': 'n-a1b2c3.example.com',
            'dns': {'provider': 'cloudflare', 'zone': 'example.com', 'ttl': 60},
            'link': c.generate_link('a' * 32, 'a1b2c3'),
            'squads': ['11111111-1111-4111-8111-111111111111'], 'mode': 'cascade-wg',
        }
        self.keys = c.wg_keys(self.store)
        key = X25519PrivateKey.generate()
        encode = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip('=')
        self.reality = {
            'private': encode(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                serialization.NoEncryption())),
            'public': encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
            'short': '1234567890abcdef',
        }
        self.node_secret = 'EU-only-service-key-123456789'

    def tearDown(self):
        self.temp.cleanup()

    def test_link_is_stable_private_and_single_host_scoped(self):
        self.assertEqual(self.plan['link'], c.generate_link('a' * 32, 'a1b2c3'))
        self.assertNotEqual(self.plan['link']['ru'], c.generate_link('b' * 32, 'a1b2c3')['ru'])
        self.assertEqual(self.plan['link']['prefix'], 32)
        self.assertEqual(self.plan['link']['interface'], 'spnwga1b2c3')
        self.assertEqual(c.validate_plan(self.plan), self.plan)
        del self.plan['link']['prefix']
        self.assertEqual(c.validate_plan(self.plan), self.plan)

    def test_format1_render_preserves_original_bytes_and_port_defaults(self):
        self.assertEqual(c.entry_port(self.plan), 443)
        self.assertEqual(c.ru_wg_port(self.plan), 51820)
        ru_files = c.role_files(self.plan, 'ru', self.keys)
        self.assertEqual(hashlib.sha256(ru_files['haproxy.cfg']).hexdigest(),
                         'b0cc57f56f97a3e92097a1556b4453cc477ce66515f8162c2b6cf0c98cddbbfc')
        for role, peer in (('ru', 'eu'), ('eu', 'ru')):
            expected = f'''# SPN encrypted cascade: {self.plan['id']} {role}
[Interface]
Address = {self.plan['link'][role]}/32
ListenPort = 51820
PrivateKey = {self.keys[role]['private']}
MTU = 1380

[Peer]
PublicKey = {self.keys[peer]['public']}
PresharedKey = {self.keys['psk']}
AllowedIPs = {self.plan['link'][peer]}/32
Endpoint = {self.plan[peer]['ip']}:51820
PersistentKeepalive = 25
'''.encode('ascii')
            files = c.role_files(self.plan, role, self.keys,
                                 node_secret=self.node_secret if role == 'eu' else None)
            self.assertEqual(files['wg.conf'], expected)
            equivalent = copy.deepcopy(self.plan)
            equivalent.update(format=2, entryPort=443)
            equivalent['link']['ruPort'] = 51820
            self.assertEqual(c.role_files(equivalent, role, self.keys,
                                         node_secret=self.node_secret if role == 'eu' else None), files)
            self.assertEqual(c.profile_config(equivalent, self.reality), c.profile_config(self.plan, self.reality))

    def test_two_independent_branches_share_ru_ip_not_ports_keys_or_private_addresses(self):
        first = copy.deepcopy(self.plan)
        first.update(format=2, entryPort=443)
        first['link']['ruPort'] = 443
        second = copy.deepcopy(first)
        second.update(id='b' * 32, code='d4e5f6', entryPort=8443, domain='n-d4e5f6.example.com')
        second['eu']['ip'] = '8.8.8.8'
        second['link'] = {**c.generate_link(second['id'], second['code']), 'ruPort': 8443}
        other_store = u.Store(self.root / 'other-branch-private-state')
        other_keys = c.wg_keys(other_store)
        self.assertEqual(first['ru']['ip'], second['ru']['ip'])
        self.assertNotEqual(first['link']['interface'], second['link']['interface'])
        self.assertNotEqual(first['link']['ru'], second['link']['ru'])
        self.assertNotEqual(first['link']['eu'], second['link']['eu'])
        for plan, keys, not_keys in ((first, self.keys, other_keys), (second, other_keys, self.keys)):
            with self.subTest(code=plan['code']):
                self.assertEqual(c.validate_plan(plan), plan)
                self.assertEqual(c.entry_port(plan), plan['entryPort'])
                self.assertEqual(c.ru_wg_port(plan), plan['link']['ruPort'])
                ru = c.role_files(plan, 'ru', keys)
                eu = c.role_files(plan, 'eu', keys, node_secret=self.node_secret)
                ru_text = b'\n'.join(ru.values()).decode()
                eu_text = b'\n'.join(eu.values()).decode()
                self.assertIn(f"bind {plan['ru']['ip']}:{plan['entryPort']}\n", ru_text)
                self.assertIn(f"ListenPort = {plan['link']['ruPort']}\n", ru_text)
                self.assertIn(f"Endpoint = {plan['eu']['ip']}:51820\n", ru_text)
                self.assertIn('ListenPort = 51820\n', eu_text)
                self.assertIn(f"Endpoint = {plan['ru']['ip']}:{plan['link']['ruPort']}\n", eu_text)
                self.assertIn(keys['ru']['private'], ru_text)
                self.assertNotIn(keys['eu']['private'], ru_text)
                self.assertIn(keys['eu']['private'], eu_text)
                self.assertNotIn(keys['ru']['private'], eu_text)
                for secret in (not_keys['ru']['private'], not_keys['eu']['private'], not_keys['psk']):
                    self.assertNotIn(secret, ru_text)
                    self.assertNotIn(secret, eu_text)
                profile = c.profile_config(plan, self.reality)
                self.assertEqual(profile['inbounds'][0]['listen'], plan['link']['eu'])
                self.assertEqual(profile['inbounds'][0]['tag'], 'cascade-' + plan['code'])
                self.assertEqual(profile, c.profile_config(copy.deepcopy(plan), self.reality))

    def test_format2_independent_tcp_and_udp_ports_and_boundary_values(self):
        for tcp, udp in ((443, 443), (1024, 1024), (65535, 65535), (8443, 51821)):
            with self.subTest(tcp=tcp, udp=udp):
                plan = copy.deepcopy(self.plan)
                plan.update(format=2, entryPort=tcp)
                plan['link']['ruPort'] = udp
                self.assertEqual(c.validate_plan(plan), plan)
                ru = c.role_files(plan, 'ru', self.keys)
                eu = c.role_files(plan, 'eu', self.keys, node_secret=self.node_secret)
                self.assertIn(f"bind {plan['ru']['ip']}:{tcp}\n".encode(), ru['haproxy.cfg'])
                self.assertIn(f'ListenPort = {udp}\n'.encode(), ru['wg.conf'])
                self.assertIn(f"Endpoint = {plan['ru']['ip']}:{udp}\n".encode(), eu['wg.conf'])

    def test_format2_supports_udp443_on_both_servers_without_changing_tcp_entry(self):
        plan = copy.deepcopy(self.plan)
        plan.update(format=2, entryPort=8443)
        plan['link'].update(ruPort=443, port=443)
        self.assertEqual(c.validate_plan(plan), plan)
        for role, peer in (('ru', 'eu'), ('eu', 'ru')):
            files = c.role_files(plan, role, self.keys,
                                 node_secret=self.node_secret if role == 'eu' else None)
            self.assertIn(b'ListenPort = 443\n', files['wg.conf'])
            self.assertIn(f"Endpoint = {plan[peer]['ip']}:443\n".encode(), files['wg.conf'])
        self.assertIn(b'bind 8.8.4.4:8443\n', c.role_files(plan, 'ru', self.keys)['haproxy.cfg'])

    def test_format2_accepts_independent_custom_eu_udp_port(self):
        for port in (1024, 8443, 51820, 65535):
            with self.subTest(port=port):
                plan = copy.deepcopy(self.plan)
                plan.update(format=2, entryPort=443)
                plan['link'].update(ruPort=10443, port=port)
                self.assertEqual(c.validate_plan(plan), plan)
                ru = c.role_files(plan, 'ru', self.keys)['wg.conf']
                eu = c.role_files(plan, 'eu', self.keys, node_secret=self.node_secret)['wg.conf']
                self.assertIn(f'Endpoint = 9.9.9.9:{port}\n'.encode(), ru)
                self.assertIn(f'ListenPort = {port}\n'.encode(), eu)
                self.assertIn(b'Endpoint = 8.8.4.4:10443\n', eu)

    def test_eu_udp_port_rejects_reserved_types_ranges_and_its_own_ssh_port(self):
        for port in (None, True, 443.0, '443', -1, 0, 80, 1023, 2222, 2443, 65536, 9443):
            with self.subTest(port=port):
                plan = copy.deepcopy(self.plan)
                plan.update(format=2, entryPort=443)
                plan['eu']['sshPort'] = 9443
                plan['link'].update(ruPort=443, port=port)
                with self.assertRaises(c.ConfigError):
                    c.validate_plan(plan)
        # A port on the *other* host is not a collision.
        plan['link']['port'] = 8443
        plan['ru']['sshPort'] = 8443
        self.assertEqual(c.validate_plan(plan), plan)

    def test_format1_eu_udp_port_remains_fixed_while_new_defaults_stay_opt_in(self):
        self.assertEqual(c.generate_link(self.plan['id'], self.plan['code'])['port'], 51820)
        for port in (443, 8443, True, '51820'):
            with self.subTest(port=port):
                plan = copy.deepcopy(self.plan)
                plan['link']['port'] = port
                with self.assertRaises(c.ConfigError):
                    c.validate_plan(plan)

    def test_plan_formats_cannot_be_implicitly_mixed_or_migrated(self):
        bad_plans = []
        with_entry = copy.deepcopy(self.plan)
        with_entry['entryPort'] = 443
        bad_plans.append(with_entry)
        with_ru_port = copy.deepcopy(self.plan)
        with_ru_port['link']['ruPort'] = 51820
        bad_plans.append(with_ru_port)
        for missing in ('entryPort', 'ruPort'):
            plan = copy.deepcopy(self.plan)
            plan.update(format=2, entryPort=8443)
            plan['link']['ruPort'] = 8443
            if missing == 'entryPort':
                del plan['entryPort']
            else:
                del plan['link']['ruPort']
            bad_plans.append(plan)
        for version in (0, 3, True, '2'):
            plan = copy.deepcopy(self.plan)
            plan['format'] = version
            bad_plans.append(plan)
        for plan in bad_plans:
            with self.subTest(version=plan.get('format'), fields=list(plan)):
                with self.assertRaises(c.ConfigError):
                    c.validate_plan(plan)

    def test_format2_ports_reject_reserved_ranges_types_and_ssh_conflicts(self):
        for name in ('entryPort', 'ruPort'):
            for port in (None, True, 443.0, '8443', -1, 0, 80, 1023, 2222, 2443, 65536, 8443):
                with self.subTest(field=name, port=port):
                    plan = copy.deepcopy(self.plan)
                    plan.update(format=2, entryPort=9443)
                    plan['ru']['sshPort'] = 8443
                    plan['link']['ruPort'] = 9443
                    if name == 'entryPort':
                        plan[name] = port
                    else:
                        plan['link'][name] = port
                    with self.assertRaises(c.ConfigError):
                        c.validate_plan(plan)

    def test_generated_keys_are_real_standard_wireguard_key_pairs(self):
        self.assertEqual(set(self.keys), {'ru', 'eu', 'psk'})
        for role in ('ru', 'eu'):
            raw = base64.b64decode(self.keys[role]['private'], validate=True)
            public = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            self.assertEqual(base64.b64encode(public).decode('ascii'), self.keys[role]['public'])
            self.assertEqual(len(raw), 32)
            self.assertEqual(len(self.keys[role]['public']), 44)
        self.assertEqual(len(base64.b64decode(self.keys['psk'], validate=True)), 32)
        self.assertNotEqual(self.keys['ru']['private'], self.keys['eu']['private'])

    def test_private_state_is_immutable_and_has_secure_permissions(self):
        path = self.store.path / c.WG_STATE
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        before = path.read_bytes()
        with patch.object(self.store, 'save', wraps=self.store.save) as save:
            self.assertEqual(c.wg_keys(self.store), self.keys)
            save.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(u.DeployError):
            self.store.save(c.WG_STATE, {**self.keys, 'psk': base64.b64encode(b'x' * 32).decode()})

    def test_insecure_key_file_permissions_are_rejected_not_repaired(self):
        path = self.store.path / c.WG_STATE
        os.chmod(path, 0o644)
        with self.assertRaises(u.DeployError):
            c.wg_keys(self.store)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_cached_bad_keys_are_rejected_without_rotation(self):
        cases = []
        bad = copy.deepcopy(self.keys)
        bad['eu']['public'] = self.keys['ru']['public']
        cases.append(bad)
        bad = copy.deepcopy(self.keys)
        bad['ru']['private'] = self.reality['private']
        cases.append(bad)
        bad = copy.deepcopy(self.keys)
        bad['psk'] = base64.b64encode(bytes(32)).decode()
        cases.append(bad)
        bad = copy.deepcopy(self.keys)
        bad['extra'] = 'not-allowed'
        cases.append(bad)
        for value in cases:
            with self.subTest(value=list(value)):
                with patch.object(self.store, 'load', return_value=value), patch.object(self.store, 'save') as save:
                    with self.assertRaises(c.ConfigError):
                        c.wg_keys(self.store)
                    save.assert_not_called()

    def test_ru_render_is_exactly_relay_files_and_has_no_eu_secrets(self):
        files = c.role_files(self.plan, 'ru', self.keys)
        self.assertEqual(set(files), {'.spn-deployment', 'wg.conf', 'haproxy.cfg'})
        self.assertEqual(files['.spn-deployment'], (self.plan['id'] + '|ru\n').encode())
        all_files = b'\n'.join(files.values()).decode()
        self.assertIn(self.keys['ru']['private'], all_files)
        self.assertNotIn(self.keys['eu']['private'], all_files)
        self.assertNotIn(self.reality['private'], all_files)
        self.assertNotIn(self.node_secret, all_files)
        for forbidden in ('node.env', 'remnawave', 'SECRET_KEY', self.plan['panel'], self.plan['panelIp'],
                          self.plan['domain'], 'CLOUDFLARE_API_TOKEN', 'REMNAWAVE_API_TOKEN'):
            self.assertNotIn(forbidden, all_files)
        with self.assertRaises(c.ConfigError):
            c.role_files(self.plan, 'ru', self.keys, node_secret=self.node_secret)

    def test_eu_render_receives_only_eu_peer_private_and_node_secret(self):
        files = c.role_files(self.plan, 'eu', self.keys, node_secret=self.node_secret)
        self.assertEqual(set(files), {'.spn-deployment', 'wg.conf', 'docker-compose.yml', 'node.env'})
        self.assertEqual(files['.spn-deployment'], (self.plan['id'] + '|eu\n').encode())
        self.assertEqual(files['node.env'], ('NODE_PORT=2222\nSECRET_KEY=' + self.node_secret + '\n').encode())
        all_files = b'\n'.join(files.values()).decode()
        self.assertIn(self.keys['eu']['private'], all_files)
        self.assertNotIn(self.keys['ru']['private'], all_files)
        self.assertNotIn(self.reality['private'], all_files)
        self.assertNotIn('haproxy', all_files)
        self.assertNotIn('REMNAWAVE_API_TOKEN', all_files)
        compose = files['docker-compose.yml'].decode()
        for required in (c.IMAGE, 'network_mode: host', 'spn.deploy.role: "eu"',
                         'soft: 65536', 'hard: 65536', 'max-size: 20m', 'max-file: \'3\''):
            self.assertIn(required, compose)
        for forbidden in ('privileged:', 'cap_add:', 'volumes:', 'docker.sock', 'ports:', 'read_only: true'):
            self.assertNotIn(forbidden, compose)

    def test_wireguard_has_only_one_host_peer_and_no_route_or_nat_hooks(self):
        for role, peer in (('ru', 'eu'), ('eu', 'ru')):
            files = c.role_files(self.plan, role, self.keys,
                                 node_secret=self.node_secret if role == 'eu' else None)
            wg = files['wg.conf'].decode()
            self.assertIn('Address = ' + self.plan['link'][role] + '/32\n', wg)
            self.assertIn('AllowedIPs = ' + self.plan['link'][peer] + '/32\n', wg)
            self.assertIn('Endpoint = ' + self.plan[peer]['ip'] + ':51820\n', wg)
            self.assertIn('PublicKey = ' + self.keys[peer]['public'] + '\n', wg)
            self.assertIn('PresharedKey = ' + self.keys['psk'] + '\n', wg)
            self.assertIn('PersistentKeepalive = 25\n', wg)
            self.assertIn('MTU = 1380\n', wg)
            self.assertEqual(wg.count('[Peer]'), 1)
            self.assertEqual(wg.count('PrivateKey = '), 1)
            for forbidden in ('0.0.0.0/0', '::/0', 'PostUp', 'PreUp', 'PostDown', 'PreDown', 'Table', 'DNS',
                              'MASQUERADE', 'SNAT', 'DNAT', 'ip_forward', '/30'):
                self.assertNotIn(forbidden, wg)

    def test_haproxy_bounds_clienthello_and_has_no_direct_fallback(self):
        cfg = c.role_files(self.plan, 'ru', self.keys)['haproxy.cfg'].decode()
        self.assertIn('bind ' + self.plan['ru']['ip'] + ':443\n', cfg)
        self.assertIn('maxconn 8192', cfg)
        self.assertIn('tune.bufsize 16384', cfg)
        self.assertIn('tcp-request inspect-delay 5s', cfg)
        self.assertIn('acl complete_client_hello req.ssl_hello_type 1', cfg)
        self.assertIn('tcp-request content accept if complete_client_hello\n', cfg)
        self.assertIn('tcp-request content reject if WAIT_END\n', cfg)
        self.assertLess(cfg.index('content accept'), cfg.index('content reject'))
        self.assertIn('server eu ' + self.plan['link']['eu'] + ':2443 send-proxy-v2 maxconn 8192\n', cfg)
        self.assertEqual(len([line for line in cfg.splitlines() if line.lstrip().startswith('server ')]), 1)
        for forbidden in ('127.0.0.1:2443', '0.0.0.0:443', self.plan['eu']['ip'], 'backup', 'use_backend',
                          'ssl crt', 'stats socket', 'stats bind', ' stats ', 'mode http'):
            self.assertNotIn(forbidden, cfg)

    def test_profile_only_listens_on_eu_private_link_and_exits_locally(self):
        profile = c.profile_config(self.plan, self.reality)
        self.assertEqual(len(profile['inbounds']), 1)
        inbound = profile['inbounds'][0]
        self.assertEqual(inbound['tag'], 'cascade-a1b2c3')
        self.assertEqual((inbound['listen'], inbound['port']), (self.plan['link']['eu'], 2443))
        self.assertEqual(inbound['protocol'], 'vless')
        self.assertEqual(inbound['settings'], {'clients': [], 'decryption': 'none'})
        stream = inbound['streamSettings']
        self.assertEqual((stream['network'], stream['security']), ('raw', 'reality'))
        self.assertEqual(stream['sockopt'], {'acceptProxyProtocol': True})
        self.assertEqual(stream['realitySettings']['privateKey'], self.reality['private'])
        self.assertEqual(stream['realitySettings']['shortIds'], [self.reality['short']])
        self.assertEqual(stream['realitySettings']['target'], c.SNI + ':443')
        self.assertEqual(stream['realitySettings']['serverNames'], [c.SNI])
        self.assertEqual(profile['outbounds'][0], {'tag': 'direct', 'protocol': 'freedom',
                                                  'settings': {'domainStrategy': 'UseIPv4'}})
        self.assertEqual(profile['routing']['rules'][0]['outboundTag'], 'block')
        self.assertIn('geoip:private', profile['routing']['rules'][0]['ip'])
        self.assertIn('100.64.0.0/10', profile['routing']['rules'][0]['ip'])
        rendered = json.dumps(profile)
        self.assertNotIn(self.plan['ru']['ip'], rendered)
        self.assertNotIn(self.plan['link']['ru'], rendered)
        self.assertNotIn('sendThrough', rendered)
        self.assertNotIn('proxySettings', rendered)

    def test_invalid_reality_keys_fail_closed(self):
        for name, value in (('private', self.keys['ru']['private']), ('public', self.reality['private']),
                            ('short', 'bad'), ('short', '1234\n5678')):
            keys = {**self.reality, name: value}
            with self.subTest(field=name):
                with self.assertRaises(c.ConfigError):
                    c.profile_config(self.plan, keys)

    def test_invalid_role_or_secret_is_not_rendered(self):
        for role, secret in (('other', None), ('eu', None), ('eu', 'short'), ('eu', 'x' * 20 + '\nINJECT=1')):
            with self.subTest(role=role):
                with self.assertRaises(c.ConfigError) as raised:
                    c.role_files(self.plan, role, self.keys, node_secret=secret)
                if secret:
                    self.assertNotIn(secret, str(raised.exception))

    def test_plan_rejects_injection_public_exposure_and_default_routes(self):
        mutations = [
            ('extra secret', lambda p: p.update(nodeSecret='never')),
            ('mode', lambda p: p.update(mode='direct')),
            ('id', lambda p: p.update(id='a' * 32 + '\n')),
            ('code', lambda p: p.update(code='ABCDEF')),
            ('same servers', lambda p: p['eu'].update(ip=p['ru']['ip'])),
            ('private endpoint', lambda p: p['eu'].update(ip='192.168.1.1')),
            ('invalid ssh', lambda p: p['ru'].update(sshPort=True)),
            ('ru ssh conflict', lambda p: p['ru'].update(sshPort=443)),
            ('eu ssh conflict', lambda p: p['eu'].update(sshPort=2222)),
            ('eu public 443', lambda p: p['eu'].update(sshPort=443)),
            ('panel credentials', lambda p: p.update(panel='https://user:secret@panel.example.com/api')),
            ('panel plain HTTP', lambda p: p.update(panel='http://panel.example.com/api')),
            ('panel query', lambda p: p.update(panel='https://panel.example.com/api?token=secret')),
            ('public link', lambda p: p['link'].update(eu='9.9.9.9')),
            ('wide prefix', lambda p: p['link'].update(prefix=30)),
            ('interface injection', lambda p: p['link'].update(interface='wg0\nPostUp=evil')),
            ('WG port', lambda p: p['link'].update(port=443)),
            ('WG MTU', lambda p: p['link'].update(mtu=1500)),
            ('route option', lambda p: p['link'].update(allowedIps='0.0.0.0/0')),
            ('domain injection', lambda p: p.update(domain='bad.example.com\n')),
            ('domain uppercase', lambda p: p.update(domain='BAD.example.com')),
            ('wrong zone', lambda p: p.update(domain='n.badexample.com')),
            ('DNS extra', lambda p: p['dns'].update(proxied=True)),
            ('DNS ttl', lambda p: p['dns'].update(ttl=300)),
            ('country injection', lambda p: p.update(country='FI\n')),
            ('name injection', lambda p: p.update(name='name\nother')),
            ('empty squads', lambda p: p.update(squads=[])),
            ('noncanonical UUID', lambda p: p.update(squads=['11111111111141118111111111111111'])),
            ('duplicate UUID', lambda p: p['squads'].append(p['squads'][0])),
        ]
        for name, mutate in mutations:
            with self.subTest(name=name):
                plan = copy.deepcopy(self.plan)
                mutate(plan)
                with self.assertRaises(c.ConfigError):
                    c.validate_plan(plan)


if __name__ == '__main__':
    unittest.main()
