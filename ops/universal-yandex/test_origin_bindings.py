"""Owned additive origin listeners with a disposable filesystem and fake OS boundary."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('origin_bindings_under_test', HERE / 'origin_bindings.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
ID, DOMAIN, CODE = 'a' * 32, 'origin.example.com', 'a1b2c3'
OLD, EXTRA = '8.8.4.4', '9.9.9.9'
BASE = (f'server {{\n    listen {OLD}:80 default_server;\n    server_name _;\n}}\n'
        f'server {{\n    listen {OLD}:443 ssl default_server;\n    server_name _;\n}}\n'
        f'server {{\n    listen {OLD}:80;\n    server_name {DOMAIN};\n}}\n'
        f'server {{\n    listen {OLD}:443 ssl http2;\n    server_name {DOMAIN};\n}}\n').encode()


class Store:
    def __init__(self, data=None):
        self.data = data or {}

    def load(self, name):
        return copy.deepcopy(self.data.get(name))


class OriginBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.calls, self.probes = [], []
        self.syntax_error = self.health_error = False
        self.addresses = [OLD, EXTRA]
        self.manager = b.Bindings(ID, DOMAIN, OLD, b.digest(BASE), root=self.root,
                                  run=self.run_command, probe=self.probe)
        m = self.manager
        m.node.mkdir(parents=True, mode=0o700)
        m.stage.mkdir(parents=True, mode=0o700)
        m.enabled.mkdir(parents=True)
        m.site.parent.mkdir(parents=True)
        self.write(m.node / '.spn-deployment', (ID + '\n').encode())
        self.params = (DOMAIN + '|' + OLD + '|1.1.1.1|22|' + ID + '\n').encode()
        self.write(m.node / '.spn-parameters', self.params)
        self.write(m.site, BASE, 0o644)
        self.write(m.stage / 'nginx.conf', BASE)
        paths = {
            '/opt/spn-yandex-cdn/node.env': b'NODE_PORT=2222\nSECRET_KEY=disposable-fixture\n',
            '/opt/spn-yandex-cdn/docker-compose.yml': b'fixture-compose\n',
            '/opt/spn-yandex-cdn/docker-daemon.json': b'{"iptables":false}\n',
            '/etc/docker/daemon.json': b'{"iptables":false}\n',
            '/etc/nginx/sites-available/spn-yandex-cdn.conf': BASE,
            '/etc/nginx/nginx.conf': b'events {}\nhttp { include /etc/nginx/sites-enabled/*; }\n',
            '/etc/letsencrypt/renewal-hooks/deploy/spn-yandex-nginx': b'#!/bin/sh\nnginx -t\nsystemctl reload nginx\n',
        }
        for name, raw in paths.items():
            mode = 0o755 if name.endswith('/spn-yandex-nginx') else 0o644 if '/etc/nginx/' in name else 0o600
            self.write(self.root / name.lstrip('/'), raw, mode)
        manifest = ''.join(b.digest(raw) + '  ' + name + '\n' for name, raw in paths.items()).encode()
        self.write(m.node / 'files.sha256', manifest)
        (m.enabled / 'spn-yandex-cdn.conf').symlink_to('/etc/nginx/sites-available/spn-yandex-cdn.conf')
        self.initial = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*')
                        if p.is_file() and not p.is_symlink()}

    def write(self, path, raw, mode=0o600):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(mode)

    def run_command(self, *args):
        self.calls.append(args)
        if args[:4] == ('ip', '-j', '-4', 'address'):
            return json.dumps([{'addr_info': [{'family': 'inet', 'local': ip} for ip in self.addresses]}]).encode()
        if args[:3] == ('nginx', '-t', '-c'):
            candidate = Path(args[3]).read_bytes()
            self.assertIn(('include ' + str(self.manager.site) + ';').encode(), candidate)
            self.assertIn(('include ' + str(self.manager.extra_file) + ';').encode(), candidate)
            if self.syntax_error:
                raise b.BindingError('candidate syntax failed')
            return b''
        if args in (('nginx', '-t'), ('systemctl', 'reload', 'nginx')):
            return b''
        raise AssertionError('Unexpected OS command: ' + repr(args))

    def probe(self, domain, ip, code):
        self.probes.append((domain, ip, code))
        if self.health_error and ip == EXTRA:
            raise b.BindingError('extra health failed')

    def install(self):
        return self.manager.install(EXTRA, CODE)

    def test_additional_binding_checks_both_addresses_and_preserves_every_original_file(self):
        result = self.install()
        self.assertTrue(result['passed'])
        self.assertTrue(result['baseConfigUnchanged'])
        self.assertEqual(self.probes, [(DOMAIN, OLD, CODE), (DOMAIN, EXTRA, CODE)])
        self.assertEqual(self.manager.verify(), result)
        self.assertEqual(os.readlink(self.manager.link), str(self.manager.extra_file))
        self.assertEqual(self.manager.extra_file.read_bytes(), BASE.replace(OLD.encode(), EXTRA.encode()))
        for name, raw in self.initial.items():
            self.assertEqual((self.root / name).read_bytes(), raw, name)
        self.assertEqual(self.manager.extra_file.stat().st_mode & 0o777, 0o600)

    def test_repeat_is_idempotent_and_cannot_choose_a_different_extra_address(self):
        first = self.install()
        before = (self.manager.directory / 'intent.json').read_bytes()
        self.assertEqual(self.install(), first)
        self.assertEqual((self.manager.directory / 'intent.json').read_bytes(), before)
        self.addresses.append('8.8.8.8')
        with self.assertRaises(b.BindingError):
            self.manager.install('8.8.8.8', CODE)
        self.assertEqual(self.manager.verify(), first)

    def test_tampered_base_or_other_owned_file_is_never_adopted(self):
        for path in (self.manager.site, self.manager.node / 'node.env'):
            with self.subTest(path=path):
                before = path.read_bytes()
                path.write_bytes(before + b'# changed\n')
                with self.assertRaises(b.BindingError):
                    self.install()
                self.assertFalse(self.manager.link.exists())
                path.write_bytes(before)

    def test_foreign_extra_symlink_and_unrelated_sites_are_preserved(self):
        foreign = self.root / 'foreign.conf'
        foreign.write_text('foreign')
        self.manager.link.symlink_to(foreign)
        with self.assertRaises(b.BindingError):
            self.install()
        self.assertEqual(os.readlink(self.manager.link), str(foreign))
        self.manager.link.unlink()
        other = self.manager.enabled / 'other.conf'
        other.symlink_to(foreign)
        with self.assertRaisesRegex(b.BindingError, 'unrelated'):
            self.install()
        self.assertEqual(os.readlink(other), str(foreign))

    def test_candidate_syntax_failure_never_activates_or_reloads(self):
        self.syntax_error = True
        with self.assertRaisesRegex(b.BindingError, 'syntax'):
            self.install()
        self.assertFalse(self.manager.link.exists())
        self.assertFalse(self.manager.link.is_symlink())
        self.assertNotIn(('systemctl', 'reload', 'nginx'), self.calls)
        self.assertEqual(self.manager.site.read_bytes(), BASE)

    def test_failed_health_removes_only_new_link_and_reload_restores_previous_site(self):
        self.health_error = True
        with self.assertRaisesRegex(b.BindingError, 'health'):
            self.install()
        self.assertFalse(self.manager.link.is_symlink())
        self.assertEqual(self.manager.site.read_bytes(), BASE)
        self.assertEqual(self.calls.count(('systemctl', 'reload', 'nginx')), 2)
        self.assertFalse((self.manager.directory / 'complete.json').exists())
        self.health_error = False
        self.assertTrue(self.install()['passed'])

    def test_unassigned_extra_ip_stops_before_journal_or_configuration_changes(self):
        self.addresses.remove(EXTRA)
        with self.assertRaisesRegex(b.BindingError, 'Both origin addresses'):
            self.install()
        self.assertFalse(self.manager.directory.exists())
        self.assertFalse(self.manager.link.is_symlink())

    def test_interrupted_journal_and_link_resume_to_the_exact_same_configuration(self):
        intent = self.manager.intent(BASE, EXTRA, CODE)
        self.manager.directory.mkdir(mode=0o700)
        b.write_once(self.manager.directory / 'intent.json', b.encoded(intent))
        b.write_once(self.manager.extra_file, b.extra_config(BASE, OLD, EXTRA))
        self.manager.link.symlink_to(self.manager.extra_file)
        result = self.install()
        self.assertEqual(self.manager.verify(), result)
        self.assertTrue((self.manager.directory / 'complete.json').exists())

    def test_local_receipt_is_bound_to_original_plan_and_exact_added_config(self):
        result = self.install()
        store = Store({'origin-bindings.receipt.json': result})
        plan = {'id': ID, 'domain': DOMAIN, 'code': CODE, 'ip': OLD}
        self.assertEqual(b.local_receipt(store, plan, BASE), result)
        for field, value in (('domain', 'other.example.com'), ('deployment', 'b' * 32),
                             ('baseSha256', '0' * 64), ('extraSha256', '0' * 64), ('passed', False)):
            with self.subTest(field=field):
                changed = {**result, field: value}
                with self.assertRaises(b.BindingError):
                    b.local_receipt(Store({'origin-bindings.receipt.json': changed}), plan, BASE)

    def test_dns_validation_is_read_only_and_rejects_changed_record_identity(self):
        receipt = self.install()
        original = {'id': 'owned-record', 'zone_id': 'zone-id', 'name': DOMAIN, 'type': 'A',
                    'content': OLD, 'proxied': False, 'ttl': 60, 'comment': 'owned-comment'}
        store, plan, zone = Store({'dns.receipt.json': original}), {'domain': DOMAIN}, {'id': 'zone-id'}
        class Client:
            def __init__(self):
                self.row = {**original, 'content': EXTRA}
                self.calls = []
            def _list(self, path, query):
                self.calls.append((path, query))
                return [self.row]
        client = Client()
        self.assertEqual(b.check_dns(client, zone, store, plan, receipt)['content'], EXTRA)
        self.assertEqual(client.calls, [('/zones/zone-id/dns_records', {'name': DOMAIN, 'match': 'all'})])
        for field, value in (('id', 'foreign'), ('content', OLD), ('comment', 'foreign'), ('proxied', True)):
            client.row = {**original, 'content': EXTRA, field: value}
            with self.subTest(field=field), self.assertRaises(b.BindingError):
                b.check_dns(client, zone, store, plan, receipt)


if __name__ == '__main__':
    unittest.main()
