"""Offline nginx/Certbot configuration checks; no service or DNS modifications."""
import argparse
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from spn_reserve_fleet.auth import Auth
from spn_reserve_fleet.web import COOKIE, create_app


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reserve_https_test', ROOT / 'spn_reserve_fleet/deploy/setup_https.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class HTTPSTests(unittest.TestCase):
    def test_dns_name_validation_rejects_nginx_injection(self):
        for invalid in ('127.0.0.1', 'IP.wayspn.com', 'ip.wayspn.com;', 'ip.wayspn.com\nserver {}',
                        '*.wayspn.com', 'ip..wayspn.com', '-ip.wayspn.com', 'ip.wayspn.com/'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                h.domain_name(invalid)

    def test_bootstrap_http_only_exposes_acme(self):
        text = h.site_text('ip.wayspn.com', tls=False)
        self.assertIn('location ^~ /.well-known/acme-challenge/', text)
        self.assertIn('return 404;', text)
        self.assertNotIn('proxy_pass', text)
        self.assertNotIn('listen 443', text)

    def test_https_preserves_application_origin_and_long_switch_requests(self):
        text = h.site_text('ip.wayspn.com', tls=True)
        for expected in ('proxy_pass http://127.0.0.1:8787;', 'proxy_set_header Host $host;',
                         'proxy_set_header X-Forwarded-Proto $scheme;',
                         'proxy_set_header X-Forwarded-For $remote_addr;',
                         'proxy_read_timeout 1000s;', 'return 301 https://ip.wayspn.com$request_uri;',
                         'ssl_certificate /etc/letsencrypt/live/ip.wayspn.com/fullchain.pem;'):
            self.assertIn(expected, text)
        self.assertNotIn('$proxy_add_x_forwarded_for', text)

    def test_dry_run_never_runs_commands(self):
        with patch.object(h, 'run', side_effect=AssertionError('dry mutation')):
            result = h.setup(argparse.Namespace(domain='ip.wayspn.com', apply=False))
        self.assertTrue(result['planOnly'])
        self.assertEqual(result['upstream'], '127.0.0.1:8787')

    def test_foreign_or_edited_site_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / 'site.conf'
            path.write_text('server { listen 443; }')
            with patch.object(h.os, 'getuid', return_value=0), patch.object(h.os, 'geteuid', return_value=0):
                # Validation is independent of whether local tests themselves
                # run as root; content mismatches always reject.
                with self.assertRaises(ValueError):
                    h.owned_file(path, (h.site_text('ip.wayspn.com', tls=True),))
            target = path.parent / 'target'
            target.write_text('other')
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                h.owned_file(path, ('other',))

    def test_renewal_hook_ignores_other_certificates(self):
        text = h.hook_text('ip.wayspn.com')
        self.assertIn('"${RENEWED_LINEAGE:-}" = "/etc/letsencrypt/live/ip.wayspn.com"', text)
        self.assertIn('/usr/sbin/nginx -t', text)
        self.assertIn('/bin/systemctl reload nginx', text)
        self.assertNotIn('restart spn-reserve', text)


class ProxyOriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_proxy_middleware_preserves_https_csrf_and_secure_cookie(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / 'private'
            root.mkdir(mode=0o700)
            class Runtime:
                auth = Auth(root / 'password.json')
            runtime = Runtime()
            runtime.auth.set_password('offline-https-test-password')
            app = ProxyHeadersMiddleware(create_app(runtime=runtime), trusted_hosts=['127.0.0.1'])
            transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 12345))
            headers = {'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '8.8.8.8'}
            async with httpx.AsyncClient(transport=transport, base_url='http://ip.wayspn.com') as client:
                session = await client.get('/api/session', headers=headers)
                self.assertIn('secure', session.headers['set-cookie'].lower())
                # The browser connects to nginx using HTTPS and sends this
                # cookie; the internal HTTP transport below emulates nginx.
                headers.update({'Origin': 'https://ip.wayspn.com',
                                'Cookie': COOKIE + '=' + client.cookies.get(COOKIE),
                                'X-CSRF-Token': session.json()['csrf']})
                logged_in = await client.post('/api/login', headers=headers,
                                              json={'password': 'offline-https-test-password'})
                self.assertEqual(logged_in.status_code, 200, logged_in.text)
                self.assertIn('secure', logged_in.headers['set-cookie'].lower())


if __name__ == '__main__':
    unittest.main()
