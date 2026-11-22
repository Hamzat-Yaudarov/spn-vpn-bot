"""Offline job lifecycle, credentials, locking and HTTPS API regressions."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import uuid

import httpx

from spn_reserve_fleet.auth import Auth
from spn_reserve_fleet.provisioning import JobStore, Provisioner, public_job
from spn_reserve_fleet.provision_worker import command_for, finish_creation, finish_replacement
from spn_reserve_fleet.runtime import Runtime
from spn_reserve_fleet.store import Store
from spn_reserve_fleet.web import create_app


SECRET = 'offline-VPS-secret-Ж🔒'
FINGERPRINT = 'SHA256:' + 'A' * 43
DOMAIN = 'fi.example.wayspn.com'
SQUAD = '11111111-1111-4111-8111-111111111111'


def form(**changes):
    value = dict(kind='create', request_id=str(uuid.uuid4()), server_id=None, ip='8.8.8.8', ssh_port=22,
                 password=SECRET, host_key_sha256=FINGERPRINT, domain='new.wayspn.com',
                 name='Финляндия', country='FI', transport='tls', squad_ids=[SQUAD], cdn_domain=None)
    value.update(changes)
    return value


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'private'
        self.store = Store(self.root)
        self.runtime = SimpleNamespace(store=self.store)
        self.launched = []
        self.manager = Provisioner(self.runtime, launch=lambda j, p: self.launched.append((j, p)))
        self.store.register(dict(domain=DOMAIN, name='Старая Финляндия', current_ip='9.9.9.9', ssh_port=22,
            transport='raw-tls-vision-safari-v1', state_dir='/private/owned-installation',
            installation_id='a'*32, panel_url='https://panel.example.com/api'))
        self.store.add_reserve(DOMAIN, '1.1.1.1')

    def tearDown(self):
        self.tmp.cleanup()

    def replacement(self, **changes):
        data = form(kind='replace', server_id=DOMAIN, domain=None, name=None, country=None,
                    transport=None, squad_ids=[])
        data.update(changes)
        return data

    def test_creation_keeps_password_only_in_launch_argument(self):
        result = self.manager.create(form())
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(self.launched, [(result['id'], SECRET)])
        raw = self.manager.jobs.get(result['id'])
        self.assertNotIn('password', json.dumps(raw))
        self.assertNotIn(SECRET, json.dumps(raw, ensure_ascii=False))
        self.assertNotIn('parameters', result)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    def test_request_retry_is_observation_and_changed_body_is_rejected(self):
        data = form()
        first = self.manager.create(data)
        second = self.manager.create(dict(data, password='another-secret'))
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.launched), 1)
        with self.assertRaises(ValueError):
            self.manager.create(dict(data, ip='8.8.4.4'))

    def test_input_rejects_injection_and_nonpublic_addresses(self):
        for changed in ({'ip': '127.0.0.1'}, {'ip': '10.0.0.1'}, {'ip': '8.8.8.8; id'},
                        {'domain': 'evil.com'}, {'domain': 'https://new.wayspn.com'},
                        {'name': 'hello\nsecret'}, {'country': 'FIN'}, {'squad_ids': []},
                        {'password': 'bad\npass'}, {'password': 'bad\x03pass'}, {'host_key_sha256': None},
                        {'ssh_port': True}, {'ssh_port': 443}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.manager.create(form(**changed))
        self.assertEqual(self.launched, [])

    def test_only_one_worker_admitted_and_target_ip_cannot_be_existing(self):
        self.manager.create(form())
        with self.assertRaises(ValueError):
            self.manager.create(form(ip='8.8.4.4', domain='two.wayspn.com'))
        for ip in ('9.9.9.9', '1.1.1.1'):
            with self.assertRaises(ValueError):
                self.manager.create(form(ip=ip))

    def test_replacement_admission_is_atomic_and_preserves_original_inventory(self):
        result = self.manager.create(self.replacement())
        current = self.store.get_server(DOMAIN)
        self.assertEqual(current['operational_job_id'], result['id'])
        self.assertFalse(current['auto_enabled'])
        self.assertEqual(current['current_ip'], '9.9.9.9')
        self.assertEqual(current['reserves'][0]['ip'], '1.1.1.1')
        source = self.manager.jobs.get(result['id'])['parameters']['source_vps']
        self.assertEqual(source['ip'], '9.9.9.9')
        self.assertEqual(source['reserves'], current['reserves'])
        with self.assertRaises(ValueError):
            self.store.add_reserve(DOMAIN, '8.8.4.4')

    def test_replacement_rejects_country_squad_or_port_change_and_archive(self):
        for change in ({'country': 'FI'}, {'squad_ids': [SQUAD]}, {'ssh_port': 2022}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.manager.create(self.replacement(**change))
        self.store.archive(DOMAIN, 'Выведен из эксплуатации')
        with self.assertRaises(ValueError):
            self.manager.create(self.replacement())

    def test_restart_retains_ownership_and_demands_explicit_resume(self):
        result = self.manager.create(self.replacement())
        restarted = Provisioner(self.runtime, launch=lambda *a: None)
        row = restarted.jobs.get(result['id'])
        self.assertEqual(row['status'], 'needs_credentials')
        self.assertEqual(self.store.get_server(DOMAIN)['operational_job_id'], result['id'])
        self.assertEqual(row['parameters']['ip'], '8.8.8.8')

    def test_resume_has_new_attempt_and_stale_worker_cannot_overwrite(self):
        result = self.manager.create(form())
        old = self.manager.jobs.get(result['id'])
        stale = JobStore(self.store, attempt_id=old['attempt_id'])
        self.manager.jobs.update(result['id'], status='failed')
        self.manager.resume(result['id'], dict(password='', certificate_target=None, cdn_target=None, finish=False))
        new = self.manager.jobs.get(result['id'])
        self.assertNotEqual(old['attempt_id'], new['attempt_id'])
        stale.update(result['id'], status='failed', error='late previous worker')
        self.assertEqual(self.manager.jobs.get(result['id'])['status'], 'queued')

    def test_cdn_manual_targets_validated_without_altering_parameters(self):
        result = self.manager.create(form(transport='cdn', cdn_domain='cdn-new.wayspn.com'))
        before = self.manager.jobs.get(result['id'])['parameters']
        self.manager.jobs.update(result['id'], status='waiting_cdn')
        with self.assertRaises(ValueError):
            self.manager.resume(result['id'], dict(password='', certificate_target='https://evil.test/x',
                                                   cdn_target=None, finish=False))
        self.manager.resume(result['id'], dict(password='', certificate_target='challenge.cm.yandexcloud.net',
                                               cdn_target=None, finish=False))
        self.assertEqual(before, self.manager.jobs.get(result['id'])['parameters'])

    def test_cli_argv_never_contains_password_and_keeps_selected_squads(self):
        result = self.manager.create(form())
        job = self.manager.jobs.get(result['id']); job['data_dir'] = str(self.root)
        command = command_for(job, '/private/job', '/private/env', '/private/subscription', '/private/control')
        self.assertNotIn(SECRET, ' '.join(command))
        self.assertEqual(command[command.index('--squad') + 1], SQUAD)
        self.assertEqual(command[command.index('--ssh-control') + 1], '/private/control')
        self.assertNotIn('--ssh-only', command)

    def test_replace_finalization_commits_job_and_unlocks_server_together(self):
        result = self.manager.create(self.replacement())
        job = self.manager.jobs.get(result['id'])
        scoped = JobStore(self.store, job['attempt_id'])
        def sync(store, state):
            return store.update(DOMAIN, current_ip='8.8.8.8', pending_ip=None)
        with patch('spn_reserve_fleet.provision_worker.synchronize', side_effect=sync):
            finish_replacement(self.store, scoped, DOMAIN, result['id'], '8.8.8.8', job['parameters']['source_vps'])
        row = self.store.get_server(DOMAIN)
        self.assertIsNone(row['operational_job_id'])
        self.assertEqual(row['reserves'], [])
        self.assertEqual(row['previous_vps'][0]['ip'], '9.9.9.9')
        self.assertEqual(row['previous_vps'][0]['reserves'][0]['ip'], '1.1.1.1')
        self.assertEqual(self.manager.jobs.get(result['id'])['status'], 'succeeded')

    def test_monitor_does_not_switch_or_probe_maintenance_server(self):
        self.manager.create(self.replacement())
        runtime = Runtime(self.root, probe=lambda *a: self.fail('SSH probe during replacement'),
                          snapshot=lambda *a: self.fail('Journal scan during replacement'))
        result = runtime.check(DOMAIN)
        self.assertTrue(result['operational_job_id'])
        with self.assertRaises(ValueError):
            runtime.switch(DOMAIN, '1.1.1.1')

    def test_new_card_is_claimed_atomically_and_finished_with_job(self):
        result = self.manager.create(form())
        job = self.manager.jobs.get(result['id'])
        self.store.register(dict(domain='new.wayspn.com', name='Новый', current_ip='8.8.8.8',
            ssh_port=22, transport='raw-tls-vision-safari-v1', state_dir='/private/new', installation_id='b'*32))
        row = self.store.get_server('new.wayspn.com')
        self.assertEqual(row['operational_job_id'], result['id'])
        with self.assertRaises(ValueError):
            self.store.add_reserve(row['id'], '8.8.4.4')
        finish_creation(self.store, JobStore(self.store, job['attempt_id']), row['id'], result['id'], '8.8.8.8')
        self.assertIsNone(self.store.get_server(row['id'])['operational_job_id'])
        self.assertEqual(self.manager.jobs.get(result['id'])['status'], 'succeeded')
        self.manager.jobs.update(result['id'], status='failed', error='late logging failure')
        self.assertEqual(self.manager.jobs.get(result['id'])['status'], 'succeeded')

    def test_cdn_transfer_uses_own_installer_and_preserves_both_domains(self):
        row = self.store.get_server(DOMAIN)
        self.store.update(DOMAIN, transport='yandex-xhttp-cdn-v1', cdn_domain='cdn-old.wayspn.com', reserves=[])
        result = self.manager.create(self.replacement())
        job = self.manager.jobs.get(result['id'])
        self.assertEqual(job['parameters']['cdn_domain'], 'cdn-old.wayspn.com')
        command = command_for(job, row['state_dir'], '/private/env', '/private/sub', '/private/control')
        self.assertTrue(any(item.endswith('universal-yandex/replace.py') for item in command))
        self.assertNotIn('--finish', command)
        self.assertNotIn('--agree-le-tos', command)
        self.assertNotIn('--cdn-domain', command)
        self.assertEqual(command[command.index('--domain') + 1], DOMAIN)

    def test_cdn_cannot_enable_direct_ip_rotation(self):
        self.store.update(DOMAIN, transport='yandex-xhttp-cdn-v1', cdn_domain='cdn-old.wayspn.com')
        for action in (lambda: self.store.add_reserve(DOMAIN, '8.8.4.4'),
                       lambda: self.store.remove_reserve(DOMAIN, '1.1.1.1'),
                       lambda: self.store.set_auto(DOMAIN, True)):
            with self.assertRaises(ValueError):
                action()


class ProvisioningWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve() / 'private'
        runtime = SimpleNamespace(store=Store(root), auth=Auth(root / 'password.json'))
        runtime.auth.set_password('offline-owner-password')
        self.app = create_app(runtime=runtime)
        self.launches = []
        self.app.state.provisioning.launcher = lambda *a: self.launches.append(a)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://ip.example.com')

    async def asyncTearDown(self):
        await self.client.aclose(); self.tmp.cleanup()

    async def login(self):
        csrf = (await self.client.get('/api/session')).json()['csrf']
        response = await self.client.post('/api/login', json={'password': 'offline-owner-password'},
                                           headers={'X-CSRF-Token': csrf})
        return {'X-CSRF-Token': response.json()['csrf']}

    async def test_auth_csrf_and_origin_protect_password_endpoint(self):
        response = await self.client.post('/api/provisioning/jobs', json=form())
        self.assertEqual(response.status_code, 403)
        headers = await self.login()
        self.assertEqual((await self.client.post('/api/provisioning/jobs', json=form())).status_code, 403)
        self.assertEqual((await self.client.post('/api/provisioning/jobs', json=form(),
            headers={**headers, 'Origin': 'https://evil.example'})).status_code, 403)
        self.assertEqual(self.launches, [])

    async def test_jobs_require_https_and_do_not_expose_private_fields(self):
        headers = await self.login()
        response = await self.client.post('/api/provisioning/jobs', json=form(), headers=headers)
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()
        listing = await self.client.get('/api/provisioning/jobs')
        self.assertEqual(listing.status_code, 200)
        for secret in (SECRET, 'parameters', 'attempt_id', 'state_dir', 'source_vps', 'host_key_sha256'):
            self.assertNotIn(secret, listing.text)
        self.assertEqual((await self.client.get('/api/provisioning/jobs/' + result['id'])).status_code, 200)
        self.client.base_url = httpx.URL('http://ip.example.com')
        response = await self.client.post('/api/provisioning/jobs', json=form(), headers=headers)
        # Secure cookie may be withheld entirely by httpx on HTTP.
        self.assertIn(response.status_code, (400, 401, 403))
        self.assertEqual(len(self.launches), 1)


if __name__ == '__main__':
    unittest.main()
