"""Pure repair transformations and private temporary files; no remote commands."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cascade_udp_repair_tests', HERE / 'repair_udp_remote.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'format': 2, 'id': 'a' * 32, 'ru': {'ip': '8.8.4.4', 'sshPort': 22},
                     'eu': {'ip': '9.9.9.9', 'sshPort': 22}, 'panelIp': '1.1.1.1', 'entryPort': 443,
                     'link': {'interface': 'spnwgabc123', 'ru': '10.253.0.1', 'eu': '10.253.0.2',
                              'port': 51820, 'ruPort': 443}}
        self.before = b'[Interface]\nPrivateKey = PRIVATE-MUST-NOT-PRINT\nListenPort = 443\n[Peer]\nEndpoint = 9.9.9.9:51820\n'
        self.after = self.before.replace(b':51820\n', b':443\n')
        self.paths = ['/opt/spn-cascade-ru/branches/' + 'a' * 32 + '/wg.conf',
                      '/etc/wireguard/spnwgabc123.conf']
        branch = {'id': 'a' * 32, 'legacy': False, 'status': 'active', 'euWGPort': 51820,
                  'ruWGPort': 443, 'euIP': '9.9.9.9', 'interface': 'spnwgabc123',
                  'files': {self.paths[0]: r.sha256(self.before), self.paths[1]: r.sha256(self.before),
                            '/etc/systemd/system/owned.service': 'b' * 64}}
        self.state = {'version': 1, 'branches': [branch, {'id': 'b' * 32, 'untouched': ['other', 'branch']}],
                      'baseline': {'/etc/ufw/ufw.conf': 'c' * 64}, 'initialized': True}

    def update(self, **kw):
        values = dict(raw=json.dumps(self.state).encode(), deployment='a' * 32,
                      old_port=51820, new_port=443, old_wg=self.before, new_wg=self.after)
        values.update(kw)
        return json.loads(r.update_ru_registry(**values))

    def test_registry_changes_only_target_udp_and_exact_owned_wg_hashes(self):
        expected = copy.deepcopy(self.state)
        expected['branches'][0]['euWGPort'] = 443
        for path in self.paths:
            expected['branches'][0]['files'][path] = r.sha256(self.after)
        self.assertEqual(self.update(), expected)

    def test_registry_rejects_key_or_listener_changes_and_hash_mismatch(self):
        for data in (self.after.replace(b'PRIVATE-MUST-NOT-PRINT', b'ROTATED'),
                     self.after.replace(b'ListenPort = 443', b'ListenPort = 8443')):
            with self.assertRaises(r.RepairError) as error:
                self.update(new_wg=data)
            self.assertNotIn('PRIVATE-MUST-NOT-PRINT', str(error.exception))
        self.state['branches'][0]['files'][self.paths[0]] = 'e' * 64
        with self.assertRaises(r.RepairError):
            self.update()

    def test_registry_rejects_ambiguous_pending_or_wrong_port(self):
        self.state['branches'].append(copy.deepcopy(self.state['branches'][0]))
        with self.assertRaises(r.RepairError):
            self.update()
        self.state['branches'].pop()
        self.state['branches'][0]['status'] = 'pending'
        with self.assertRaises(r.RepairError):
            self.update()
        self.state['branches'][0]['status'] = 'active'
        with self.assertRaises(r.RepairError):
            self.update(old_port=8443)

    def test_eu_parameter_transition_matches_bash_optional_suffix(self):
        prefix = 'eu|9.9.9.9|8.8.4.4|1.1.1.1|22|' + 'a' * 32 + '|spnwgabc123|10.253.0.2|10.253.0.1|'
        self.assertEqual(r.eu_parameters(self.plan), prefix + '51820|443|443')
        self.plan['link']['port'] = 443
        self.assertEqual(r.eu_parameters(self.plan), prefix + '443')
        self.plan['entryPort'] = 8443
        self.assertEqual(r.eu_parameters(self.plan), prefix + '443|8443|443')

    def test_ru_preflight_always_has_entry_and_ru_udp_suffix(self):
        prefix = 'ru|8.8.4.4|9.9.9.9|1.1.1.1|22|' + 'a' * 32 + '|spnwgabc123|10.253.0.1|10.253.0.2|'
        self.assertEqual(r.ru_preflight(self.plan), prefix + '51820|443|443')
        self.plan['link']['port'] = 443
        self.assertEqual(r.ru_preflight(self.plan), prefix + '443|443|443')

    def test_manifest_snapshot_and_atomic_replace_have_no_external_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in r.UFW_SOURCES:
                path = root / name.lstrip('/')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'owned firewall fixture\n')
                path.chmod(0o644)
            manifest = r.firewall_manifest(root, uid=os.getuid()).decode()
            self.assertEqual(len(manifest.splitlines()), 8)
            self.assertEqual([line.split('  ', 1)[1] for line in manifest.splitlines()], list(r.UFW_SOURCES))
            private = root / 'private'
            private.write_bytes(self.before); private.chmod(0o600)
            snapshot = r.snapshot_file(private, uid=os.getuid())
            self.assertEqual(snapshot['sha256'], r.sha256(self.before))
            self.assertTrue(r.atomic_private_replace(private, self.before, self.after, uid=os.getuid()))
            self.assertFalse(r.atomic_private_replace(private, self.before, self.after, uid=os.getuid()))
            with self.assertRaises(r.RepairError):
                r.atomic_private_replace(private, self.before, b'new', uid=os.getuid())
            private.chmod(0o644)
            with self.assertRaises(r.RepairError):
                r.snapshot_file(private, uid=os.getuid())


if __name__ == '__main__':
    unittest.main()
