"""TLS transport and certificate lifecycle contracts without live resources."""
import copy
import contextlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('tls_install_helpers', HERE / 'test_deploy.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
d = helpers.d
DOMAIN = 'de11-1-1.wayspn.com'


class TLSTests(unittest.TestCase):
    def setUp(self):
        self.fixture = helpers.InstallerTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def args(self, *extra):
        return d.parser().parse_args(['--ip', helpers.IP, '--domain', DOMAIN, '--squad', 'regular', *extra])

    def plan(self, store=None, args=None):
        return self.fixture.plan(store=store, args=args or self.args())

    def test_default_is_tls_with_domain_identity_and_no_reality_private_key(self):
        store = self.fixture.store()
        plan = self.plan(store)
        self.assertTrue(d.is_tls(plan))
        self.assertEqual(plan['domain'], DOMAIN)
        self.assertFalse((store.path / 'reality.private.json').exists())
        keys, options = d.probe_parameters(plan, store)
        self.assertEqual(keys, {'public': '', 'short': ''})
        self.assertEqual(options, {'sni': DOMAIN, 'security': 'tls'})
        self.assertEqual(plan['tlsSiteVersion'], 1)

    def test_tls_site_routes_each_negotiated_http_protocol_to_loopback(self):
        store = self.fixture.store()
        plan = self.plan(store)
        inbound = d.profile_body(plan, store)['config']['inbounds'][0]
        self.assertEqual(inbound['settings']['fallbacks'], [
            {'dest': '127.0.0.1:18080'}, {'alpn': 'h2', 'dest': '127.0.0.1:18081'}])
        self.assertEqual(inbound['streamSettings']['tlsSettings']['alpn'], ['h2', 'http/1.1'])
        self.assertIs(inbound['streamSettings']['tlsSettings']['rejectUnknownSni'], True)
        files = d.render_files(plan, 'test-service-secret-123456789012345')
        self.assertIn(b'listen 127.0.0.1:18080;', files['tls-site-nginx.conf'])
        self.assertIn(b'listen 127.0.0.1:18081 http2;', files['tls-site-nginx.conf'])
        self.assertNotIn(b'listen 80', files['tls-site-nginx.conf'])
        self.assertIn(b'RuntimeDirectory=spn-tls-site-' + plan['id'].encode(), files['tls-site.service'])

    def legacy_tls(self):
        plan = self.plan(self.fixture.store('new-template'))
        plan.pop('tlsSiteVersion')
        store = self.fixture.store('legacy-tls')
        store.save('plan.json', plan)
        deployment = d.Deployment(self.fixture.api, store, plan)
        profile, node, inbound = deployment.provision()
        return plan, store, deployment, profile, node, inbound

    def test_legacy_tls_config_and_plan_stay_byte_identical_before_explicit_upgrade(self):
        plan, store, _, profile, _, _ = self.legacy_tls()
        before = (store.path / 'plan.json').read_bytes()
        self.assertNotIn('fallbacks', profile['config']['inbounds'][0]['settings'])
        self.assertEqual(profile['config']['inbounds'][0]['streamSettings']['tlsSettings']['alpn'], ['h2'])
        self.assertNotIn('rejectUnknownSni', profile['config']['inbounds'][0]['streamSettings']['tlsSettings'])
        self.assertEqual(d.tls_site_version(plan, store), 0)
        self.assertEqual(d.tls_runtime_plan(plan, store), plan)
        self.assertNotIn('tls-site-index.html', d.render_files(plan, 'test-service-secret-123456789012345'))
        self.assertEqual((store.path / 'plan.json').read_bytes(), before)

    def test_explicit_tls_upgrade_preserves_original_plan_intent_uuids_and_resumes(self):
        plan, store, deployment, profile, node, inbound = self.legacy_tls()
        original = {name: (store.path / name).read_bytes() for name in ('plan.json', 'profile.intent.json')}
        upgraded = d.upgrade_tls_site_profile(self.fixture.api, store, plan)
        self.assertEqual(upgraded['uuid'], profile['uuid'])
        self.assertEqual(upgraded['inbounds'][0]['uuid'], inbound)
        self.assertEqual(upgraded['name'], profile['name'])
        self.assertEqual(d.tls_runtime_plan(plan, store)['tlsSiteVersion'], 1)
        self.assertEqual(upgraded['config'], d.profile_body(plan, store)['config'])
        self.assertEqual(deployment.provision()[0], upgraded)
        self.assertEqual(d.upgrade_tls_site_profile(self.fixture.api, store, plan), upgraded)
        for name, data in original.items():
            self.assertEqual((store.path / name).read_bytes(), data)

    def test_explicit_tls_upgrade_refuses_shared_or_changed_profile_before_claim(self):
        plan, store, _, profile, node, _ = self.legacy_tls()
        self.fixture.api.data['nodes'].append({**node, 'uuid': 'another-node'})
        with self.assertRaises(d.u.DeployError):
            d.upgrade_tls_site_profile(self.fixture.api, store, plan)
        self.assertIsNone(store.load('tls-site-v1.intent.json'))
        self.fixture.api.data['nodes'].pop()
        self.fixture.api.data['config-profiles'][0]['config']['inbounds'][0]['port'] = 8443
        with self.assertRaises(d.u.DeployError):
            d.upgrade_tls_site_profile(self.fixture.api, store, plan)
        self.assertIsNone(store.load('tls-site-v1.intent.json'))

    def test_explicit_tls_upgrade_recovers_lost_patch_response(self):
        plan, store, _, profile, _, _ = self.legacy_tls()
        original_call = self.fixture.api.__call__
        class LostPatch:
            rows = self.fixture.api.rows
            def __call__(self, method, path, body=None):
                result = original_call(method, path, body)
                if method == 'PATCH' and path == '/config-profiles':
                    raise d.u.DeployError('Simulated lost PATCH response')
                return result
        with self.assertRaises(d.u.DeployError):
            d.upgrade_tls_site_profile(LostPatch(), store, plan)
        self.assertIsNotNone(store.load('tls-site-v1.intent.json'))
        self.assertIsNone(store.load('tls-site-v1.receipt.json'))
        upgraded = d.upgrade_tls_site_profile(self.fixture.api, store, plan)
        self.assertEqual(upgraded['uuid'], profile['uuid'])
        self.assertIsNotNone(store.load('tls-site-v1.receipt.json'))

    def test_resume_never_silently_migrates_legacy_or_tls_transport(self):
        for transport in ('tls', 'reality'):
            with self.subTest(transport=transport):
                store = self.fixture.store(transport)
                original = self.plan(store, self.args('--transport', transport))
                self.assertEqual(self.plan(store), original)
                opposite = 'reality' if transport == 'tls' else 'tls'
                with self.assertRaises(d.u.DeployError):
                    self.plan(store, self.args('--transport', opposite))

    def test_tls_profile_host_and_container_share_certificate_identity(self):
        store = self.fixture.store()
        plan = self.plan(store)
        deployment = d.Deployment(self.fixture.api, store, plan)
        profile, node, inbound = deployment.provision()
        host = deployment.host(profile, node, inbound)
        stream = profile['config']['inbounds'][0]['streamSettings']
        self.assertEqual(stream['security'], 'tls')
        self.assertNotIn('realitySettings', stream)
        self.assertEqual(host['sni'], DOMAIN)
        self.assertEqual(host['securityLayer'], 'TLS')
        self.assertEqual(node['address'], DOMAIN)
        self.assertEqual(stream['tlsSettings']['certificates'][0]['certificateFile'],
                         '/etc/letsencrypt/live/' + DOMAIN + '/fullchain.pem')
        files = d.render_files(plan, 'test-service-secret-123456789012345')
        self.assertIn(b'/etc/letsencrypt:/etc/letsencrypt:ro', files['docker-compose.yml'])
        self.assertIn(b'certbot.timer', files['managed-node.sh'])
        self.assertNotIn('guard.py', files)

    def test_full_installer_reuses_validated_tls_options_and_enrolls(self):
        self.fixture.install(self.args('--apply'))
        profile = self.fixture.api.data['config-profiles'][0]
        self.assertEqual(profile['config']['inbounds'][0]['streamSettings']['security'], 'tls')
        self.assertEqual(self.fixture.api.data['hosts'][0]['isDisabled'], False)
        _, options = self.fixture.export_calls[0]
        self.assertEqual((options['sni'], options['security']), (DOMAIN, 'tls'))
        self.assertFalse(any(path.name == 'reality.private.json' for path in self.fixture.root.rglob('*')))

    def test_tls_client_checks_certificate_even_through_owned_ssh_forward(self):
        config = d.probe.client_config(helpers.IP, '', '', '00000000-0000-4000-8000-000000000001',
                                       19001, 'en0', DOMAIN, 'ssh', 19002, security='tls')
        out = config['outbounds'][0]
        self.assertEqual(out['settings']['vnext'][0]['address'], '127.0.0.1')
        self.assertEqual(out['streamSettings']['tlsSettings']['serverName'], DOMAIN)
        self.assertIs(out['streamSettings']['tlsSettings']['allowInsecure'], False)
        self.assertEqual(out['settings']['vnext'][0]['users'][0]['flow'], 'xtls-rprx-vision')

    def test_export_rejects_insecure_tls_wrong_domain_and_wrong_transport(self):
        out = d.probe.client_config(helpers.IP, '', '', '00000000-0000-4000-8000-000000000001',
                                   19001, 'en0', DOMAIN, 'system', security='tls')['outbounds'][0]
        out['settings']['vnext'][0]['address'] = DOMAIN
        with patch.object(d.probe, 'fetch_documents', return_value={'outbounds': [out]}):
            proof = d.probe.verify_export(helpers.IP, '', '', 'unused', sni=DOMAIN, security='tls', endpoint_address=DOMAIN)
        self.assertTrue(proof['passed'])
        for changed in ('insecure', 'sni', 'security'):
            bad = copy.deepcopy(out)
            if changed == 'insecure':
                bad['streamSettings']['tlsSettings']['allowInsecure'] = True
            elif changed == 'sni':
                bad['streamSettings']['tlsSettings']['serverName'] = 'other.wayspn.com'
            else:
                bad['streamSettings']['security'] = 'none'
            with self.subTest(changed=changed), patch.object(d.probe, 'fetch_documents', return_value={'outbounds': [bad]}), \
                    self.assertRaises(d.probe.ProbeError):
                d.probe.verify_export(helpers.IP, '', '', 'unused', sni=DOMAIN, security='tls', endpoint_address=DOMAIN)

    def test_tls_sni_cannot_be_ip_or_command_text(self):
        for name in ('8.8.8.8', 'localhost', 'example.com;id', 'example.com\nx'):
            with self.subTest(name=name), self.assertRaises(d.probe.ProbeError):
                d.probe.validate_sni(name, 'tls')

    def test_renew_hook_is_scoped_to_certificate_and_owned_container(self):
        script = (HERE / 'tls-node.sh').read_text()
        syntax = subprocess.run(['bash', '-n', str(HERE / 'tls-node.sh')], capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn('--standalone --preferred-challenges http', script)
        self.assertIn('systemctl enable --now certbot.timer', script)
        hook = script.split('cat > "$STAGE/cert-renew-hook" <<HOOK\n')[1].split('\nHOOK', 1)[0]
        self.assertIn('RENEWED_LINEAGE', hook)
        self.assertIn('spn.deploy.id', hook)
        self.assertLess(hook.index('spn.deploy.id'), hook.index('docker restart'))
        self.assertIn('-verify_hostname "$CERT_DOMAIN"', script)

    def site_wait_helper(self):
        script = (HERE / 'tls-node.sh').read_text()
        names = ('site_public_pages_ready', 'wait_site_public_pages')
        return ''.join(re.search(r'^' + name + r'\(\) \{\n.*?^\}\n', script, re.M | re.S).group()
                       for name in names)

    def test_https_page_wait_retries_failed_delivery_and_checks_both_protocols(self):
        with tempfile.TemporaryDirectory(prefix='site-wait-') as directory:
            root = Path(directory)
            (root / 'index.html').write_text('temporary expected page')
            program = 'set -euo pipefail\nSITE=' + shlex.quote(str(root)) + '\nCERT_DOMAIN=' + DOMAIN + '\nNODE_IP=8.8.4.4\n'
            program += self.site_wait_helper()
            program += '''
curl() {
    local attempt=0
    [ ! -f "$SITE/attempts" ] || read -r attempt < "$SITE/attempts"
    attempt=$((attempt+1)); printf '%s\\n' "$attempt" > "$SITE/attempts"
    printf '%s\\n' "$*" >> "$SITE/calls"
    [ "$attempt" -ge 3 ] || return 28
    cat "$SITE/index.html"
}
sleep() { printf 'retry\\n' >> "$SITE/retries"; }
wait_site_public_pages
'''
            result = subprocess.run(['bash', '-c', program], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'retries').read_text().splitlines(), ['retry', 'retry'])
            calls = (root / 'calls').read_text().splitlines()
            self.assertEqual(len(calls), 4)
            self.assertIn('--http1.1', calls[2])
            self.assertIn('--http2', calls[3])
            self.assertTrue(all('--proto =https' in call and '--resolve ' + DOMAIN + ':443:8.8.4.4' in call for call in calls))
            self.assertTrue(all('--insecure' not in call for call in calls))

    def test_https_page_wait_stops_at_elapsed_deadline_without_sleeping(self):
        program = 'set -euo pipefail\n' + self.site_wait_helper() + '''
curl() { SECONDS=$((SECONDS+46)); return 28; }
cmp() { return 1; }
# Pipeline commands run in subshells; force elapsed time in the outer predicate.
site_public_pages_ready() { SECONDS=$((SECONDS+46)); return 1; }
sleep() { echo 'deadline incorrectly slept' >&2; exit 91; }
if wait_site_public_pages; then exit 92; fi
'''
        result = subprocess.run(['bash', '-c', program], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_batch_ssh_never_prompts_automated_controller(self):
        plan = self.plan()
        plan['batchMode'] = True
        ssh = d.SSH(plan)
        try:
            self.assertIn('BatchMode=yes', ssh.common)
            self.assertIn('NumberOfPasswordPrompts=0', ssh.common)
            with patch.object(ssh, 'run') as run:
                ssh.action('preflight')
            self.assertEqual(shlex.split(run.call_args.args[0])[-1], DOMAIN)
        finally:
            ssh.tmp.cleanup()


class CertificateShellTests(unittest.TestCase):
    """Execute the shipped shell verifier against real disposable X.509 chains."""
    @classmethod
    def setUpClass(cls):
        candidates = [shutil.which('openssl'), '/opt/homebrew/opt/openssl@3/bin/openssl', '/usr/local/opt/openssl@3/bin/openssl']
        cls.openssl = None
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                help_result = subprocess.run([candidate, 'verify', '-help'], capture_output=True)
                if b'-verify_hostname' in help_result.stdout + help_result.stderr:
                    cls.openssl = candidate
                    break
        if cls.openssl is None:
            raise unittest.SkipTest('Certificate integration checks require OpenSSL with verify_hostname (Ubuntu 24.04 provides it)')
        cls.tmp = tempfile.TemporaryDirectory(prefix='spn-tls-cert-test-')
        cls.root = Path(cls.tmp.name).resolve()
        cls.stage = cls.root / 'stage'
        cls.stage.mkdir()
        cls.live = cls.root / 'letsencrypt' / 'live' / DOMAIN
        cls.live.mkdir(parents=True)
        cls.raw_script = (HERE / 'tls-node.sh').read_text()
        cls.helper = re.search(r'^certificate_fresh\(\) \{\n.*?^\}\n', cls.raw_script, re.M | re.S).group()
        cls.helper += re.search(r'^verify_certificate\(\) \{\n.*?^\}\n', cls.raw_script, re.M | re.S).group()
        cls.helper = cls.helper.replace('/etc/letsencrypt', str(cls.root / 'letsencrypt'))
        cls.helper = cls.helper.replace('-CApath /etc/ssl/certs', '-CAfile ' + shlex.quote(str(cls.root / 'root.pem')))
        cls.command('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                    '-keyout', 'root.key', '-out', 'root.pem', '-days', '10', '-subj', '/CN=Disposable Root')
        cls.command('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                    '-keyout', 'intermediate.key', '-out', 'intermediate.csr', '-subj', '/CN=Disposable Intermediate')
        (cls.root / 'intermediate.ext').write_text('basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n')
        cls.command('x509', '-req', '-in', 'intermediate.csr', '-CA', 'root.pem', '-CAkey', 'root.key',
                    '-CAcreateserial', '-out', 'intermediate.pem', '-days', '8', '-extfile', 'intermediate.ext')
        cls.command('req', '-new', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                    '-keyout', 'leaf.key', '-out', 'leaf.csr', '-subj', '/CN=' + DOMAIN)
        (cls.root / 'leaf.ext').write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:' + DOMAIN + '\n')
        for days in (1, 5):
            cls.command('x509', '-req', '-in', 'leaf.csr', '-CA', 'intermediate.pem', '-CAkey', 'intermediate.key',
                        '-CAcreateserial', '-out', 'leaf-' + str(days) + '.pem', '-days', str(days), '-extfile', 'leaf.ext')
        # Use a deterministic twelve-hour validity, avoiding a boundary at
        # exactly 24h when OpenSSL signs and checks within the same second.
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        leaf = x509.load_pem_x509_certificate((cls.root / 'leaf-1.pem').read_bytes())
        signer = serialization.load_pem_private_key((cls.root / 'intermediate.key').read_bytes(), password=None)
        now = datetime.now(timezone.utc)
        builder = x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(leaf.issuer)
        builder = builder.public_key(leaf.public_key()).serial_number(x509.random_serial_number())
        builder = builder.not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(hours=12))
        for extension in leaf.extensions:
            builder = builder.add_extension(extension.value, extension.critical)
        (cls.root / 'leaf-1.pem').write_bytes(builder.sign(signer, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))

    @classmethod
    def command(cls, *args):
        result = subprocess.run([cls.openssl, *args], cwd=cls.root, capture_output=True)
        if result.returncode:
            raise AssertionError('Disposable certificate setup failed: ' + result.stderr.decode())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def verify(self, *, days=5, domain=DOMAIN, intermediate=True, wrong_key=False):
        chain = (self.root / ('leaf-' + str(days) + '.pem')).read_bytes()
        if intermediate:
            chain += (self.root / 'intermediate.pem').read_bytes()
        (self.live / 'fullchain.pem').write_bytes(chain)
        (self.live / 'privkey.pem').write_bytes((self.root / ('intermediate.key' if wrong_key else 'leaf.key')).read_bytes())
        helper = self.helper
        # Keep the certificate location fixed when deliberately testing hostname mismatch.
        helper = helper.replace('/live/$CERT_DOMAIN', '/live/' + DOMAIN)
        program = 'set -euo pipefail\nfail() { echo "$*" >&2; exit 1; }\n'
        program += 'CERT_DOMAIN=' + shlex.quote(domain) + '\nSTAGE=' + shlex.quote(str(self.stage)) + '\n'
        program += helper + '\nverify_certificate\n'
        return subprocess.run(['bash', '-c', program], capture_output=True, text=True,
                              env={**os.environ, 'PATH': str(Path(self.openssl).parent) + os.pathsep + os.environ.get('PATH', '')})

    def test_real_leaf_intermediate_root_chain_and_key_pass(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_missing_intermediate_and_wrong_hostname_are_rejected(self):
        for kwargs in ({'intermediate': False}, {'domain': 'other.wayspn.com'}):
            with self.subTest(kwargs=kwargs):
                result = self.verify(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('chain/domain is not trusted', result.stderr)

    def test_real_mismatched_private_key_is_rejected(self):
        result = self.verify(wrong_key=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('private key differs', result.stderr)

    def test_real_certificate_with_less_than_one_day_remaining_is_rejected(self):
        result = self.verify(days=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('expires within one day', result.stderr)

    def test_actual_rendered_renewal_hook_only_restarts_owned_lineage(self):
        template = re.search(r'    cat > "\$STAGE/cert-renew-hook" <<HOOK\n.*?^HOOK\n', self.raw_script, re.M | re.S).group()
        deploy_id = 'a' * 32
        for case in ('owned', 'other-certificate', 'other-node', 'other-container'):
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix='hook-', dir=self.root) as directory:
                root = Path(directory)
                node = root / 'node'
                node.mkdir()
                (node / '.spn-deployment').write_text(('b' * 32 if case == 'other-node' else deploy_id) + '\n')
                setup = 'set -eu\nSTAGE=' + shlex.quote(str(root)) + '\nNODE=' + shlex.quote(str(node))
                setup += '\nCERT_DOMAIN=' + DOMAIN + '\nDEPLOY_ID=' + deploy_id + '\n' + template
                rendered = subprocess.run(['bash', '-c', setup], capture_output=True, text=True)
                self.assertEqual(rendered.returncode, 0, rendered.stderr)
                fake = root / 'docker'
                fake.write_text('#!/bin/sh\nset -eu\nprintf "%s\\n" "$*" >> "$HOOK_LOG"\nif [ "$1" = inspect ]; then printf "%s\\n" "$HOOK_OWNER"; fi\n')
                fake.chmod(0o700)
                log = root / 'docker.log'
                environment = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ.get('PATH', ''),
                               'HOOK_LOG': str(log), 'HOOK_OWNER': 'b' * 32 if case == 'other-container' else deploy_id,
                               'RENEWED_LINEAGE': '/etc/letsencrypt/live/' + ('other.wayspn.com' if case == 'other-certificate' else DOMAIN)}
                result = subprocess.run(['sh', str(root / 'cert-renew-hook')], capture_output=True, text=True, env=environment)
                calls = log.read_text() if log.exists() else ''
                if case == 'owned':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('restart remnanode', calls)
                else:
                    self.assertNotIn('restart', calls)
                    self.assertEqual(result.returncode, 0 if case == 'other-certificate' else 1, result.stderr)


class XrayFallbackIntegrationTests(unittest.TestCase):
    """Optional real engine checks, with disposable credentials and loopback only."""
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get('SPN_TEST_XRAY')
        if not configured or not Path(configured).is_file():
            raise unittest.SkipTest('Set SPN_TEST_XRAY to an installed Xray binary for loopback integration checks')
        cls.engine = str(Path(configured).resolve())

    @staticmethod
    def port():
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    @staticmethod
    def wait_ready(process, port):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError('Disposable Xray did not start: ' + process.communicate()[0].decode(errors='replace'))
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    return
            except OSError:
                time.sleep(.025)
        raise AssertionError('Disposable Xray listener did not become ready')

    def test_http1_http2_fallback_and_authenticated_vless_share_real_tls_listener(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        page = b'<!doctype html><title>Field notes</title><p>Temporary local fixture.</p>'
        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            def log_message(self, *args):
                pass

        processes, threads = [], []
        with tempfile.TemporaryDirectory(prefix='xray-site-') as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, DOMAIN)])
            now = datetime.now(timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                    .not_valid_after(now + timedelta(days=2)).add_extension(x509.SubjectAlternativeName([x509.DNSName(DOMAIN)]), False)
                    .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
            (root / 'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            (root / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))
            http = ThreadingHTTPServer(('127.0.0.1', 0), Page)
            stack.callback(http.server_close)
            stack.callback(http.shutdown)
            worker = threading.Thread(target=http.serve_forever, daemon=True)
            worker.start()
            threads.append(worker)
            h2_listener = stack.enter_context(socket.socket())
            h2_listener.bind(('127.0.0.1', 0))
            h2_listener.listen()
            h2_listener.settimeout(5)
            h2_received = []
            def receive_h2():
                try:
                    connection, _ = h2_listener.accept()
                    with connection:
                        connection.settimeout(3)
                        h2_received.append(connection.recv(1024))
                        connection.sendall(b'h2-fallback-confirmed')
                except OSError as error:
                    h2_received.append(error)
            worker = threading.Thread(target=receive_h2, daemon=True)
            worker.start()
            threads.append(worker)
            server_port, client_port = self.port(), self.port()
            credential = str(uuid.uuid4())
            config = d.tls_profile_config('site01', DOMAIN, site_version=1)
            config['log']['loglevel'] = 'debug'
            inbound = config['inbounds'][0]
            inbound.update(listen='127.0.0.1', port=server_port)
            inbound['settings']['clients'] = [{'id': credential, 'flow': 'xtls-rprx-vision'}]
            inbound['settings']['fallbacks'][0]['dest'] = '127.0.0.1:' + str(http.server_port)
            inbound['settings']['fallbacks'][1]['dest'] = '127.0.0.1:' + str(h2_listener.getsockname()[1])
            inbound['streamSettings']['tlsSettings']['certificates'] = [{
                'certificateFile': str(root / 'cert.pem'), 'keyFile': str(root / 'key.pem')}]
            config['routing']['rules'] = []  # Disposable fixture intentionally reaches loopback.
            # Xray 26.9.9 also blocks private destinations in Freedom itself.
            # Grant just this disposable loopback backend in the test fixture.
            config['outbounds'][0]['settings']['finalRules'] = [
                {'action': 'allow', 'ip': ['127.0.0.1/32'], 'port': str(http.server_port)}]
            def start(config, filename, port):
                path = root / filename
                path.write_text(json.dumps(config))
                checked = subprocess.run([self.engine, 'run', '-test', '-config', str(path)], capture_output=True)
                self.assertEqual(checked.returncode, 0, checked.stderr.decode(errors='replace'))
                process = subprocess.Popen([self.engine, 'run', '-config', str(path)], stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT)
                processes.append(process)
                stack.callback(stop, process)
                self.wait_ready(process, port)
                return process
            def stop(process):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            start(config, 'server.json', server_port)
            # Disable client-side certificate checks for these negative tests:
            # only a server-side rejection can make the handshake fail.
            for server_name in ('unrelated.example.invalid', None):
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                context.set_alpn_protocols(['http/1.1'])
                with self.subTest(rejected_sni=server_name), socket.create_connection(('127.0.0.1', server_port), timeout=3) as raw:
                    with self.assertRaises(ssl.SSLError):
                        with context.wrap_socket(raw, server_hostname=server_name):
                            pass
            def tls(protocol):
                context = ssl.create_default_context(cafile=str(root / 'cert.pem'))
                context.set_alpn_protocols([protocol])
                raw = socket.create_connection(('127.0.0.1', server_port), timeout=3)
                return context.wrap_socket(raw, server_hostname=DOMAIN)
            with tls('http/1.1') as connection:
                self.assertEqual(connection.selected_alpn_protocol(), 'http/1.1')
                connection.sendall(b'GET / HTTP/1.1\r\nHost: ' + DOMAIN.encode() + b'\r\nConnection: close\r\n\r\n')
                response = b''
                while not response.endswith(page):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    response += chunk
                self.assertIn(b'200 OK', response)
                self.assertTrue(response.endswith(page))
            h2_preface = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n' + b'\0\0\0\x04\0\0\0\0\0'
            with tls('h2') as connection:
                self.assertEqual(connection.selected_alpn_protocol(), 'h2')
                connection.sendall(h2_preface)
                self.assertEqual(connection.recv(128), b'h2-fallback-confirmed')
            self.assertEqual(h2_received, [h2_preface])
            client = {
                'log': {'loglevel': 'debug'},
                'inbounds': [{'listen': '127.0.0.1', 'port': client_port, 'protocol': 'socks', 'settings': {'auth': 'noauth'}}],
                'outbounds': [{'protocol': 'vless', 'settings': {'vnext': [{'address': '127.0.0.1', 'port': server_port,
                              'users': [{'id': credential, 'encryption': 'none', 'flow': 'xtls-rprx-vision'}]}]},
                              'streamSettings': {'network': 'raw', 'security': 'tls', 'tlsSettings': {
                                  'serverName': DOMAIN, 'fingerprint': 'safari', 'alpn': ['h2'],
                                  'certificates': [{'usage': 'verify', 'certificateFile': str(root / 'cert.pem')}],
                              }}}],
            }
            start(client, 'client.json', client_port)
            with socket.create_connection(('127.0.0.1', client_port), timeout=3) as connection:
                connection.sendall(b'\x05\x01\x00')
                self.assertEqual(connection.recv(2), b'\x05\x00')
                connection.sendall(b'\x05\x01\x00\x01\x7f\x00\x00\x01' + http.server_port.to_bytes(2, 'big'))
                handshake = connection.recv(10)
                self.assertEqual(handshake[:2], b'\x05\x00')
                connection.sendall(b'GET / HTTP/1.1\r\nHost: local.test\r\nConnection: close\r\n\r\n')
                response = b''
                try:
                    while not response.endswith(page):
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        response += chunk
                except TimeoutError:
                    logs = []
                    for process in processes:
                        process.terminate()
                        logs.append(process.communicate(timeout=3)[0].decode(errors='replace'))
                    self.fail('Authenticated VLESS fixture timed out: ' + '\n'.join(logs))
                self.assertIn(b'200 OK', response)
                self.assertTrue(response.endswith(page))
        for worker in threads:
            worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
