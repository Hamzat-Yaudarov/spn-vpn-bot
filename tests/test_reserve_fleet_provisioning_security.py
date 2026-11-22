"""Offline security boundaries for provisioning pipes, PTYs and attempt fencing."""
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import termios
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from spn_reserve_fleet.provisioning import JobStore, Provisioner, public_job
from spn_reserve_fleet.provision_ssh import PasswordMaster, validate_password
from spn_reserve_fleet import provision_worker as worker
from spn_reserve_fleet.store import Store


JOB_ID = 'a' * 32
ATTEMPT = 'b' * 32
SECRET = 'secret-$(touch NEVER);`literal`-"quoted"'


def job_value(**changes):
    parameters = {'kind':'create', 'server_id':None, 'ip':'8.8.8.8', 'ssh_port':22,
        'host_key_sha256':'SHA256:' + 'A' * 43, 'domain':'secure.wayspn.com', 'name':'Тест',
        'country':'FI', 'transport':'tls', 'squad_ids':['c' * 32], 'cdn_domain':None}
    value = {'id':JOB_ID, 'attempt_id':ATTEMPT, 'parameters':parameters, 'request':{},
        'status':'queued', 'kind':'create', 'server_id':None, 'domain':parameters['domain'],
        'target_ip':parameters['ip'], 'transport':'tls', 'created_at':1, 'updated_at':1}
    value.update(changes)
    return value


class RecordingPipe(io.BytesIO):
    def close(self):
        self.delivered = self.getvalue()
        super().close()


class ProvisioningSecurity(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / 'fleet'
        self.store = Store(self.root)
        self.jobs = JobStore(self.store)
        self.jobs.insert(job_value())

    def tearDown(self):
        self.temporary.cleanup()

    def capture_launch(self):
        child = SimpleNamespace(stdin=RecordingPipe(), wait=lambda:0)
        launches, reapers = [], []
        def popen(command, **kwargs):
            launches.append((command, kwargs))
            return child
        def thread(**kwargs):
            reapers.append(kwargs['target'])
            return SimpleNamespace(start=lambda:None)
        provisioner = Provisioner(SimpleNamespace(store=self.store), launch=lambda *args:None, recover=False)
        with patch('spn_reserve_fleet.provisioning.subprocess.Popen', side_effect=popen), \
             patch('spn_reserve_fleet.provisioning.threading.Thread', side_effect=thread):
            provisioner._launch(JOB_ID, SECRET)
        return child, launches, reapers

    def test_root_password_uses_only_stdin_pipe_and_private_files_contain_no_password(self):
        child, launches, _ = self.capture_launch()
        command, kwargs = launches[0]
        self.assertNotIn(SECRET, repr(command))
        self.assertNotIn(SECRET, repr(kwargs.get('env', {})))
        self.assertEqual(json.loads(child.stdin.delivered), {'password':SECRET})
        self.assertTrue(kwargs['close_fds'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(command[command.index('--attempt-id') + 1], ATTEMPT)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), str(path))
                self.assertNotIn(json.dumps(SECRET)[1:-1].encode(), path.read_bytes(), str(path))

    def test_stale_reaper_cannot_fail_new_attempt(self):
        _, _, reapers = self.capture_launch()
        next_attempt = 'd' * 32
        self.jobs.update(JOB_ID, status='running', attempt_id=next_attempt, stage='new-attempt')
        reapers[0]()
        state = self.jobs.get(JOB_ID)
        self.assertEqual((state['status'], state['attempt_id'], state['stage']),
                         ('running', next_attempt, 'new-attempt'))

    def test_stale_worker_updates_are_fenced_at_sqlite_write(self):
        scoped = JobStore(self.store, attempt_id=ATTEMPT)
        next_attempt = 'e' * 32
        self.jobs.update(JOB_ID, status='running', attempt_id=next_attempt, progress=42)
        scoped.update(JOB_ID, status='succeeded', progress=100, error=SECRET)
        state = self.jobs.get(JOB_ID)
        self.assertEqual(state['status'], 'running')
        self.assertEqual(state['progress'], 42)
        self.assertNotIn(SECRET, json.dumps(state))

    def test_replace_admission_rolls_back_maintenance_claim_when_job_insert_fails(self):
        server_id = 'source.wayspn.com'
        self.store.register({'domain':server_id, 'name':'Source', 'current_ip':'1.1.1.1',
            'state_dir':'/private/existing-installation', 'installation_id':'existing',
            'ssh_port':22, 'transport':'raw-tls-vision-safari-v1'})
        self.store.update(server_id, auto_enabled=True)
        self.jobs.update(JOB_ID, status='succeeded')
        with self.store.connection() as conn:
            conn.execute("CREATE TRIGGER fail_job BEFORE INSERT ON provisioning_jobs "
                         "BEGIN SELECT RAISE(ABORT, 'offline injected write failure'); END")
        candidate = job_value(id='f' * 32, kind='replace', server_id=server_id)
        candidate['parameters'].update(kind='replace', server_id=server_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.jobs.insert(candidate)
        row = self.store.get_server(server_id)
        self.assertIsNone(row.get('operational_job_id'))
        self.assertTrue(row['auto_enabled'])
        self.assertEqual(len(self.jobs.list()), 1)

    def test_public_job_excludes_private_request_parameters_and_unknown_credentials(self):
        raw = job_value(request={'password':SECRET, 'token':SECRET}, password=SECRET,
            parameters={'state_dir':'/private/' + SECRET, 'password':SECRET}, worker_log=SECRET,
            cdn={'origin_domain':'origin.wayspn.com', 'cdn_domain':'cdn.wayspn.com',
                 'guide_text':'Инструкция', 'api_token':SECRET, 'private_key':SECRET})
        result = public_job(raw)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn('parameters', result)
        self.assertNotIn('request', result)
        self.assertEqual(result['cdn']['guide_text'], 'Инструкция')

    def test_same_request_with_new_password_is_observation_not_second_login(self):
        request = {'request_id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'ip':'8.8.8.8'}
        self.jobs.update(JOB_ID, request=request, status='failed')
        launches = []
        provisioner = Provisioner(SimpleNamespace(store=self.store), launch=lambda *args:launches.append(args), recover=False)
        result = provisioner.create({**request, 'password':SECRET})
        self.assertEqual(result['id'], JOB_ID)
        self.assertEqual(launches, [])
        with self.assertRaises(ValueError):
            provisioner.create({**request, 'ip':'1.1.1.1', 'password':SECRET})
        self.assertNotIn(SECRET, json.dumps(self.jobs.get(JOB_ID)))

    def test_failed_installer_never_installs_key_reconciles_or_declares_success(self):
        observed = []
        def execute(*args, **kwargs):
            # The worker must hold this lock even for a create job.
            with self.assertRaises(ValueError):
                with self.store.server_lock('provisioning-global'):
                    self.fail('Worker lost the global provisioning lock')
            observed.append(True)
            return 2, ('provider secret: ' + SECRET).encode()
        fake_d = SimpleNamespace(u=SimpleNamespace(Store=lambda path:object()),
                                 trust=SimpleNamespace(resolve=lambda *args, **kwargs:self.root / 'known'))
        class Master:
            control = '/private/control.sock'
            def __enter__(self): return self
            def __exit__(self, *args): return False
        args = SimpleNamespace(data_dir=self.root, job_id=JOB_ID, attempt_id=ATTEMPT,
                               env_file=self.root / 'private.env')
        with patch.object(worker, 'replacement_module', return_value=SimpleNamespace(d=fake_d)), \
             patch.object(worker, 'PasswordMaster', return_value=Master()), \
             patch.object(worker, 'execute', side_effect=execute), \
             patch.object(worker, 'install_key') as install_key, \
             patch.object(worker, 'synchronize') as synchronize:
            worker.run(args, SECRET)
        self.assertEqual(observed, [True])
        install_key.assert_not_called()
        synchronize.assert_not_called()
        result = self.jobs.get(JOB_ID)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn(SECRET, json.dumps(public_job(result)))
        self.assertLess(result['progress'], 100)

    def test_worker_credential_pipe_timeout_is_bounded_and_never_starts_installation(self):
        argv = ['provision_worker', '--data-dir', str(self.root), '--job-id', JOB_ID,
                '--attempt-id', ATTEMPT, '--env-file', str(self.root / 'private.env')]
        fake_stdin = SimpleNamespace(buffer=object(), fileno=lambda:99)
        output = io.StringIO()
        with patch.object(worker.sys, 'argv', argv), patch.object(worker.sys, 'stdin', fake_stdin), \
             patch.object(worker.sys, 'stdout', output), patch.object(worker.os, 'umask'), \
             patch.object(worker.select, 'select', return_value=([], [], [])) as select_pipe, \
             patch.object(worker, 'run') as run:
            self.assertEqual(worker.main(), 1)
        run.assert_not_called()
        timeout = select_pipe.call_args.args[3]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 15)
        self.assertEqual(self.jobs.get(JOB_ID)['status'], 'failed')
        self.assertNotIn(SECRET, output.getvalue())

    def test_worker_accepts_full_unicode_password_from_bounded_utf8_pipe(self):
        password = '🔐' * 1024
        payload = json.dumps({'password':password}, ensure_ascii=False).encode()
        argv = ['provision_worker', '--data-dir', str(self.root), '--job-id', JOB_ID,
                '--attempt-id', ATTEMPT, '--env-file', str(self.root / 'private.env')]
        fake_stdin = SimpleNamespace(buffer=object(), fileno=lambda:99)
        with patch.object(worker.sys, 'argv', argv), patch.object(worker.sys, 'stdin', fake_stdin), \
             patch.object(worker.os, 'umask'), \
             patch.object(worker.select, 'select', return_value=([fake_stdin.buffer], [], [])), \
             patch.object(worker.os, 'read', side_effect=[payload[:100], payload[100:], b'']), \
             patch.object(worker, 'run') as run:
            self.assertEqual(worker.main(), 0)
        self.assertEqual(run.call_args.args[1], password)


class PrivatePTY(unittest.TestCase):
    def test_shell_punctuation_is_literal_password_not_argv_environment_or_echo(self):
        observations, threads, calls = {}, [], []
        class Child:
            pid = 987654321
            done = False
            def poll(self): return 0 if self.done else None
            def wait(self, timeout=None): self.done = True; return 0
        child = Child()
        def popen(command, **kwargs):
            calls.append((command, kwargs))
            self.assertFalse(termios.tcgetattr(kwargs['stdin'])[3] & termios.ECHO)
            slave = os.dup(kwargs['stdin'])
            control = command[command.index('-S') + 1]
            def guest():
                try:
                    os.write(slave, b"root@8.8.8.8's password: ")
                    observations['password'] = os.read(slave, 4096)
                    Path(control).touch()
                finally:
                    os.close(slave)
            thread = threading.Thread(target=guest, daemon=True)
            threads.append(thread); thread.start()
            return child
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as patches:
            patches.enter_context(patch('spn_reserve_fleet.provision_ssh.subprocess.Popen', side_effect=popen))
            patches.enter_context(patch('spn_reserve_fleet.provision_ssh.subprocess.run', return_value=SimpleNamespace(returncode=0)))
            patches.enter_context(patch('spn_reserve_fleet.provision_ssh.os.killpg'))
            with PasswordMaster('8.8.8.8', 22, Path(temporary) / 'known_hosts', Path(temporary) / 'key', SECRET, timeout=3) as master:
                self.assertEqual(master.password, '')
                self.assertEqual(os.stat(Path(master.control).parent).st_mode & 0o777, 0o700)
            for thread in threads: thread.join(timeout=1)
        self.assertEqual(observations['password'], SECRET.encode() + b'\n')
        command, kwargs = calls[0]
        self.assertNotIn(SECRET, repr(command))
        self.assertNotIn(SECRET, repr(kwargs['env']))
        self.assertIn('IdentityAgent=none', command)
        self.assertIn('ForwardAgent=no', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('GlobalKnownHostsFile=/dev/null', command)

    def test_multiline_null_and_oversized_passwords_are_rejected(self):
        for value in ('line\nnext', 'line\rnext', 'nul\x00byte', 'a' * 1025, None, 123):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError): validate_password(value)
        self.assertEqual(validate_password(SECRET), SECRET)


if __name__ == '__main__':
    unittest.main()
