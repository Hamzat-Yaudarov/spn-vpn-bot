"""Linux controller probes use pinned native engines without Mac path claims."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
import zipfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('linux_vpn_probe_under_test', HERE / 'vpn_probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
IP = '8.8.4.4'
USER = '00000000-0000-4000-8000-000000000001'
PUBLIC, SHORT = 'A' * 43, '0123456789abcdef'


class LinuxProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(p.platform, 'system', return_value='Linux'))
        self.stack.enter_context(patch.object(p.platform, 'machine', return_value='x86_64'))

    def tearDown(self):
        self.stack.close()
        self.temporary.cleanup()

    def test_native_architecture_selects_distinct_fixed_official_assets(self):
        for machine, architecture in [('x86_64', 'amd64'), ('amd64', 'amd64'),
                                       ('aarch64', 'arm64'), ('arm64', 'arm64')]:
            with self.subTest(machine=machine), patch.object(p.platform, 'machine', return_value=machine):
                selected = p.engine_spec()
                asset, binary, archive = p.LINUX_ENGINES[architecture]
                self.assertEqual(selected['sha256'], binary)
                self.assertEqual(selected['archiveSha256'], archive)
                self.assertEqual(selected['url'], f'https://github.com/XTLS/Xray-core/releases/download/v26.9.9/{asset}')
                self.assertEqual(selected['cacheName'], f'xray-26.9.9-linux-{architecture}')
                self.assertIsNone(selected['existing'])

    def test_unsupported_architecture_fails_before_read_download_or_execution(self):
        with patch.object(p.platform, 'machine', return_value='riscv64'), \
                patch.object(p.e, 'pinned_engine', side_effect=AssertionError('must not inspect')) as pin, \
                patch.object(p, '_download_engine', side_effect=AssertionError('must not download')) as download:
            with self.assertRaises(p.ProbeError):
                p.select_engine(self.root / 'custom-engine', home=self.root)
        pin.assert_not_called()
        download.assert_not_called()

    def test_custom_engine_also_requires_native_linux_digest(self):
        expected = p.LINUX_ENGINES['amd64'][1]
        with patch.object(p.e, 'pinned_engine') as pin:
            custom = self.root / 'engine'
            self.assertEqual(p.select_engine(custom), custom)
        pin.assert_called_once_with(custom, expected)

    def test_linux_cache_never_reuses_darwin_engine_or_existing_mac_temporary(self):
        cache = self.root / '.spn-reality-deploy/runtime'
        cache.mkdir(parents=True, mode=0o700)
        (cache / ('xray-' + p.VERSION)).write_bytes(b'wrong platform')
        selected = cache / p.engine_spec()['cacheName']
        with patch.object(p, '_download_engine', return_value=selected) as download, \
                patch.object(p.e, 'pinned_engine', side_effect=AssertionError('must not inspect Mac engine')):
            self.assertEqual(p.select_engine(home=self.root), selected)
        download.assert_called_once_with(cache)

    def test_tampered_native_cache_does_not_trigger_download_or_mac_fallback(self):
        cache = self.root / '.spn-reality-deploy/runtime'
        cache.mkdir(parents=True, mode=0o700)
        target = cache / p.engine_spec()['cacheName']
        target.write_bytes(b'wrong binary')
        target.chmod(0o700)
        with patch.object(p, '_download_engine', side_effect=AssertionError('must not replace bad cache')) as download:
            with self.assertRaises(p.ProbeError):
                p.select_engine(home=self.root)
        download.assert_not_called()

    def _archive(self, binary):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as zipped:
            zipped.writestr('xray', binary)
        return stream.getvalue()

    def test_download_checks_both_archive_and_native_binary_before_atomic_install(self):
        binary = b'test-only native executable bytes; never run'
        archive = self._archive(binary)
        selected = {**p.engine_spec(), 'sha256': hashlib.sha256(binary).hexdigest(),
                    'archiveSha256': hashlib.sha256(archive).hexdigest()}
        cache = self.root / 'cache'
        cache.mkdir(mode=0o700)
        def download(args, **kwargs):
            self.assertEqual(args[-1], selected['url'])
            Path(args[args.index('--output') + 1]).write_bytes(archive)
            return types.SimpleNamespace(returncode=0)
        with patch.object(p, 'engine_spec', return_value=selected), \
                patch.object(p.subprocess, 'run', side_effect=download) as run:
            target = p._download_engine(cache)
        self.assertEqual(target, cache / selected['cacheName'])
        self.assertEqual(target.read_bytes(), binary)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual(run.call_count, 1)  # curl only; downloaded content never executes here.

    def test_download_rejects_archive_and_binary_digest_mismatch_before_install(self):
        archive = self._archive(b'untrusted bytes')
        cache = self.root / 'cache'
        cache.mkdir(mode=0o700)
        original = p.engine_spec()
        def download(args, **kwargs):
            Path(args[args.index('--output') + 1]).write_bytes(archive)
            return types.SimpleNamespace(returncode=0)
        for archive_digest in ('0' * 64, hashlib.sha256(archive).hexdigest()):
            selected = {**original, 'archiveSha256': archive_digest}
            with patch.object(p, 'engine_spec', return_value=selected), \
                    patch.object(p.subprocess, 'run', side_effect=download), \
                    self.assertRaises(p.ProbeError):
                p._download_engine(cache)
            self.assertFalse((cache / selected['cacheName']).exists())

    def test_linux_system_accepts_unused_lo_label_without_binding_or_direct_fallback(self):
        config = p.client_config(IP, PUBLIC, SHORT, USER, 19999, 'lo', route='system')
        self.assertEqual(len(config['outbounds']), 1)
        self.assertEqual(config['outbounds'][0]['protocol'], 'vless')
        self.assertNotIn('sockopt', config['outbounds'][0]['streamSettings'])
        self.assertNotIn('freedom', json.dumps(config))
        self.assertEqual(config['outbounds'][0]['settings']['vnext'][0]['address'], IP)
        with self.assertRaises(p.ProbeError):
            p.client_config(IP, PUBLIC, SHORT, USER, 19999, 'lo', route='physical')

    def test_linux_physical_mode_stops_before_engine_or_subscription(self):
        with patch.object(p, 'select_engine', side_effect=AssertionError('must not run')) as engine, \
                patch.object(p, 'fetch_credential', side_effect=AssertionError('must not fetch')) as fetch:
            with self.assertRaises(p.ProbeError):
                p.run(IP, PUBLIC, SHORT, self.root / 'secret', interface='eth0', route='physical')
        engine.assert_not_called()
        fetch.assert_not_called()

    def test_linux_fingerprint_is_native_engine_bound_but_not_unused_interface(self):
        fingerprint = p.connection_fingerprint(IP, PUBLIC, SHORT, 'lo', route='system')
        self.assertEqual(fingerprint, p.connection_fingerprint(IP, PUBLIC, SHORT, 'eth0', route='system'))
        with patch.object(p.platform, 'system', return_value='Darwin'), \
                patch.object(p.platform, 'machine', return_value='arm64'):
            darwin = p.connection_fingerprint(IP, PUBLIC, SHORT, 'en0', route='system')
        self.assertNotEqual(fingerprint, darwin)

    def test_linux_system_runs_all_nine_tests_in_private_tmp_and_never_claims_mac_observation(self):
        child = types.SimpleNamespace(poll=lambda: None, pid=123)
        reservation = MagicMock()
        reservation.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 19999)
        real_temporary_directory = tempfile.TemporaryDirectory
        def temporary_directory(*args, **kwargs):
            self.assertEqual(kwargs['dir'], '/tmp')
            return real_temporary_directory(*args, **kwargs)
        with patch.object(p, 'select_engine', return_value=Path('/unused/test-engine')), \
                patch.object(p, 'fetch_credential', return_value=USER), \
                patch.object(p, 'physical_tcp_preflight', side_effect=AssertionError('Mac preflight')) as physical, \
                patch.object(p.subprocess, 'check_output', side_effect=AssertionError('Mac command')) as mac, \
                patch.object(p.socket, 'socket', return_value=reservation), \
                patch.object(p.socket, 'create_connection', return_value=MagicMock()), \
                patch.object(p.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
                patch.object(p.subprocess, 'Popen', return_value=child), \
                patch.object(p, 'physical_observed', side_effect=AssertionError('Mac lsof')) as observed, \
                patch.object(p, 'request', return_value={'passed': True, 'clientAlive': True}) as request, \
                patch.object(p.tempfile, 'TemporaryDirectory', side_effect=temporary_directory), \
                patch.object(p.e, 'stop') as stop:
            proof = p.run(IP, PUBLIC, SHORT, self.root / 'secret', interface='lo', route='system')
        self.assertTrue(proof['passed'])
        self.assertEqual(proof['tests'], 9)
        self.assertEqual(request.call_count, 9)
        self.assertEqual(proof['testRoute'], 'system')
        self.assertIsNone(proof['physicalTcp'])
        self.assertIsNone(proof['physicalInterface'])
        self.assertFalse(proof['directReachabilityVerified'])
        self.assertFalse(proof['physicalSourceObserved'])
        physical.assert_not_called()
        mac.assert_not_called()
        observed.assert_not_called()
        stop.assert_called_once_with(child)


if __name__ == '__main__':
    unittest.main()
