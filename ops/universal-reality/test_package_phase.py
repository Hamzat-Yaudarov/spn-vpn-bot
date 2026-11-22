"""Package hooks finish before the installer owns a preferred-source route."""
import copy
import importlib.util
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('package_phase_install_helpers', HERE / 'test_deploy.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def function(script, name):
    matches = re.findall(r'^' + re.escape(name) + r'\(\) \{\n.*?^\}\n', script, re.M | re.S)
    if len(matches) != 1:
        raise AssertionError('Expected one actual shell function: ' + name)
    return matches[0]


class PackageOrderTests(unittest.TestCase):
    def test_networkd_package_restart_precedes_saved_route_for_both_transports(self):
        for transport in ('reality', 'tls'):
            with self.subTest(transport=transport):
                fixture = helpers.InstallerTests()
                fixture.setUp()
                route = {'protocol': 'boot'}
                snapshot = None
                packages_done = False
                original_source = helpers.FakeSSH.source

                def package_hooks(action):
                    nonlocal packages_done
                    if action == 'packages':
                        # Reproduce needrestart/networkd restoring the configured
                        # static route while installation dependencies are added.
                        route['protocol'] = 'static'
                        packages_done = True
                    elif action == 'install' and not packages_done:
                        route['protocol'] = 'static'

                def source(ssh, action, capture=False):
                    nonlocal snapshot
                    if action == 'apply':
                        snapshot = copy.deepcopy(route)
                    elif action == 'verify':
                        self.assertEqual(route, snapshot, 'Packages changed an already saved route')
                    return original_source(ssh, action, capture=capture)

                try:
                    helpers.FakeSSH.on_action = package_hooks
                    with patch.object(helpers.FakeSSH, 'source', source):
                        fixture.install(fixture.args('--transport', transport, '--apply'))
                    calls = helpers.FakeSSH.calls
                    self.assertTrue(packages_done)
                    self.assertEqual(snapshot, {'protocol': 'static'})
                    self.assertLess(calls.index(('action', 'preflight')), calls.index(('action', 'packages')))
                    self.assertLess(calls.index(('action', 'packages')), calls.index(('source', 'check')))
                    self.assertLess(calls.index(('action', 'packages')), calls.index(('source', 'apply')))
                    first_upload = next(item[1] for item in calls if item[0] == 'upload')
                    self.assertEqual('tls-site-policy.sh' in first_upload, transport == 'tls')
                    self.assertNotIn('node.env', first_upload)
                finally:
                    fixture.tearDown()


class ShellPackageTests(unittest.TestCase):
    def run_packages(self, script_name, *, marker=False, missing='', site_hook=False, twice=False):
        script = (HERE / script_name).read_text()
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            if marker:
                (stage / 'packages-ok').touch(mode=0o600)
            program = 'set -euo pipefail\numask 077\nSTAGE=' + shlex.quote(str(stage)) + '\nSITE_VERSION=1\n'
            program += 'MISSING=' + shlex.quote(missing) + '\n'
            program += '''
# Commands only record calls: no APT, daemon or network operation can run.
command() { if [ "$1" = -v ]; then [ "$2" != "$MISSING" ]; else return 99; fi; }
safe_file() { [ -f "$1" ] && [ ! -L "$1" ]; }
apt_update() { echo apt-update; }
apt_install() { printf 'apt-install %s\\n' "$*"; MISSING=; }
systemctl() { printf 'systemctl %s\\n' "$*"; }
docker() { printf 'docker %s\\n' "$*"; }
'''
            if script_name == 'tls-node.sh':
                if site_hook:
                    program += 'install_site_packages() { echo site-packages; }\n'
                else:
                    # With nginx already available, the actual site-package
                    # helper must return without any package/service operation.
                    program += function(script, 'install_site_packages')
            program += function(script, 'prepare_packages')
            program += '\nprepare_packages\n'
            if twice:
                program += 'prepare_packages\n'
            result = subprocess.run(['bash', '-c', program], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((stage / 'packages-ok').is_file())
            return result.stdout.splitlines()

    def test_existing_marker_and_available_dependencies_do_not_run_apt(self):
        for name in ('managed-node.sh', 'tls-node.sh'):
            with self.subTest(script=name):
                self.assertEqual(self.run_packages(name, marker=True), [])

    def test_real_package_helpers_install_exact_dependencies_once_and_resume(self):
        expected = {
            'managed-node.sh': 'ca-certificates curl python3 docker.io docker-compose-v2 ufw haproxy',
            'tls-node.sh': 'ca-certificates curl python3 docker.io docker-compose-v2 ufw certbot openssl',
        }
        for name, packages in expected.items():
            with self.subTest(script=name):
                self.assertEqual(self.run_packages(name, twice=True), [
                    'apt-update', 'apt-install ' + packages, 'systemctl enable --now docker'])

    def test_missing_dependency_is_repaired_even_with_completion_marker(self):
        for name, missing in (('managed-node.sh', 'haproxy'), ('tls-node.sh', 'openssl')):
            with self.subTest(script=name):
                output = self.run_packages(name, marker=True, missing=missing)
                self.assertEqual(output[0], 'apt-update')
                self.assertIn(missing, output[1].split())

    def test_tls_package_phase_also_finishes_nginx_setup(self):
        output = self.run_packages('tls-node.sh', site_hook=True)
        self.assertEqual(output[-1], 'site-packages')
        self.assertLess(output.index('apt-update'), output.index('site-packages'))

    def test_both_shell_actions_parse_and_keep_install_package_fallback(self):
        for name in ('managed-node.sh', 'tls-node.sh'):
            with self.subTest(script=name):
                script = (HERE / name).read_text()
                result = subprocess.run(['bash', '-n', str(HERE / name)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                packages = re.search(r'^packages\)\n(.*?)^    ;;', script, re.M | re.S).group(1)
                self.assertLess(packages.index('safe_file "$STAGE/preflight-ok"'), packages.index('prepare_packages'))
                install = re.search(r'^install\)\n(.*?)^    ;;', script, re.M | re.S).group(1)
                self.assertIn('prepare_packages', install)


if __name__ == '__main__':
    unittest.main()
