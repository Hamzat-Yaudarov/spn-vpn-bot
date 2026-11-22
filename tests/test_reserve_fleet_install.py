"""Offline bundle smoke checks; no package installation, SSH, or service changes."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'spn_reserve_fleet/deploy' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bundle = module('reserve_fleet_bundle_test', 'build_bundle.py')
installer = module('reserve_fleet_installer_test', 'install.py')


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.archive = self.root / 'runtime.tar.gz'
        self.result = bundle.build(self.archive)
        self.extracted = self.root / 'extracted'
        self.extracted.mkdir()
        with tarfile.open(self.archive) as archive:
            archive.extractall(self.extracted, filter='data')

    def tearDown(self):
        self.temporary.cleanup()

    def test_archive_has_only_runtime_sources_and_complete_transitive_imports(self):
        with tarfile.open(self.archive) as archive:
            names = archive.getnames()
        self.assertIn('ops/host-health-audit/e2e.py', names)
        self.assertIn('ops/universal-direct/deploy.py', names)
        self.assertIn('ops/universal-reality/tls-node.sh', names)
        self.assertIn('spn_reserve_fleet/static/index.html', names)
        self.assertTrue(all('test_' not in name and '.env' not in name and 'private' not in name
                            and '__pycache__' not in name for name in names))
        self.assertEqual(self.archive.stat().st_mode & 0o777, 0o600)
        script = '''import sys
sys.path.insert(0, sys.argv[1])
from spn_reserve_fleet.enrollment import replacement_module
module = replacement_module()
assert module.d.is_tls({'transport':'raw-tls-vision-safari-v1'})
print('runtime dependencies imported')
'''
        completed = subprocess.run([sys.executable, '-B', '-c', script, str(self.extracted)],
                                   cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn('dependencies imported', completed.stdout)

    def test_source_hash_validation_and_changed_source_is_rejected(self):
        files, digest = installer.source_files(self.extracted)
        self.assertEqual(digest, self.result['sourceHash'])
        self.assertGreater(len(files), 25)
        path = self.extracted / 'spn_reserve_fleet/runtime.py'
        path.write_text(path.read_text() + '\n# changed\n')
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            installer.source_files(self.extracted)

    def test_manifest_cannot_include_secret_or_escape_paths_or_symlink(self):
        manifest_file = self.extracted / 'runtime-manifest.json'
        original = json.loads(manifest_file.read_text())
        for name in ('../private.key', '/root/.ssh/id_ed25519', '.env', 'ops/other.py'):
            with self.subTest(name=name):
                manifest = {**original, 'files': {**original['files'], name: 'a' * 64}}
                manifest_file.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    installer.source_files(self.extracted)
        manifest_file.write_text(json.dumps(original))
        source = self.extracted / 'spn_reserve_fleet/runtime.py'
        raw = source.read_bytes()
        source.unlink()
        elsewhere = self.root / 'runtime.py'
        elsewhere.write_bytes(raw)
        source.symlink_to(elsewhere)
        with self.assertRaisesRegex(ValueError, 'Unsafe source file'):
            installer.source_files(self.extracted)

    def test_dry_install_does_not_invoke_commands_or_create_target(self):
        target = self.root / 'never-created'
        args = argparse.Namespace(source=str(self.extracted), apply=False, start=True)
        with patch.object(installer, 'INSTALL', target), \
                patch.object(installer, 'run', side_effect=AssertionError('No mutation in dry-run')):
            result = installer.install(args)
        self.assertTrue(result['planOnly'])
        self.assertFalse(result['startsService'])
        self.assertFalse(target.exists())

    def test_service_is_loopback_and_runs_as_own_account_with_private_home(self):
        text = installer.service_text()
        self.assertIn('User=spn-reserve', text)
        self.assertIn('Environment=HOME=/var/lib/spn-reserve-fleet', text)
        self.assertIn('--host 127.0.0.1 --port 8787', text)
        self.assertIn('/opt/spn-reserve-fleet/venv/bin/python', text)
        self.assertIn('UMask=0077', text)
        self.assertNotIn('0.0.0.0', text)

    def test_foreign_install_and_foreign_systemd_unit_are_not_adopted(self):
        target = self.root / 'foreign-install'
        target.mkdir()
        unit = self.root / 'foreign.service'
        unit.write_text('[Service]\nExecStart=/bin/other\n')
        with patch.object(installer, 'INSTALL', target):
            with self.assertRaisesRegex(ValueError, 'another installer'):
                installer.marker()
        with patch.object(installer, 'UNIT', unit):
            with self.assertRaisesRegex(ValueError, 'not owned'):
                installer.validate_unit()

    def test_private_ssh_umask_does_not_hide_new_runtime_sources_and_venv(self):
        install = self.root / 'install'
        previous = os.umask(0o077)
        try:
            with installer.public_code_umask():
                installer.root_directory(install, owner_uid=os.geteuid())
                release = install / 'releases/new/spn_reserve_fleet/static'
                release.mkdir(mode=0o755, parents=True)
                (release / 'index.html').write_text('readable UI')
                subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(install / 'venv')],
                               check=True, capture_output=True, timeout=30)
            current = os.umask(0o077)
            self.assertEqual(current, 0o077, 'Installer must restore the calling umask')
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(install.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((release / 'index.html').stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((install / 'venv/pyvenv.cfg').stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((install / 'venv/lib').stat().st_mode), 0o755)

    def test_existing_private_runtime_tree_is_repaired_without_touching_home_or_symlink_targets(self):
        install = self.root / 'install'
        home = self.root / 'private-home'
        previous = os.umask(0o077)
        try:
            home.mkdir(mode=0o700)
            password = home / 'password.json'
            password.write_text('private hash')
            release = install / 'releases/old/spn_reserve_fleet'
            release.mkdir(mode=0o755, parents=True)
            (release / 'runtime.py').write_text('existing code')
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(install / 'venv')],
                           check=True, capture_output=True, timeout=30)
            executable = install / 'venv/bin/tool'
            executable.write_text('#!/bin/sh\n')
            executable.chmod(0o700)
            (install / 'venv/private-target-link').symlink_to(password)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((install / 'venv/pyvenv.cfg').stat().st_mode), 0o600)
        installer.root_directory(install, owner_uid=os.geteuid())
        installer.root_directory(install / 'releases', owner_uid=os.geteuid())
        installer.repair_public_code_tree(install / 'releases', owner_uid=os.geteuid())
        installer.repair_public_code_tree(install / 'venv', owner_uid=os.geteuid())
        self.assertEqual(stat.S_IMODE(release.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((release / 'runtime.py').stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((install / 'venv/pyvenv.cfg').stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(password.stat().st_mode), 0o600)
        self.assertTrue((install / 'venv/private-target-link').is_symlink())

    def test_runtime_permission_repair_refuses_hardlinks_without_changing_their_targets(self):
        public = self.root / 'public-code'
        public.mkdir()
        private = self.root / 'private.txt'
        private.write_text('not runtime')
        private.chmod(0o600)
        os.link(private, public / 'hardlink.py')
        with self.assertRaisesRegex(ValueError, 'Unsafe public code'):
            installer.repair_public_code_tree(public, owner_uid=os.geteuid())
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
