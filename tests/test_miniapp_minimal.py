"""MiniApp-only source contracts plus an opt-in fully intercepted browser suite."""
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class MinimalMiniAppContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'static/miniapp/index.html').read_text()
        cls.js = (ROOT / 'static/miniapp/assets/app.js').read_text()
        cls.css = (ROOT / 'static/miniapp/assets/app.css').read_text()
        cls.document = Document(cls.html)

    def test_four_navigation_destinations_and_no_separate_dashboard(self):
        tabs = [a for t, a in self.document.tags if t == 'button' and 'tab' in a.get('class', '').split()]
        self.assertEqual([a.get('data-view') for a in tabs], ['subs', 'buy', 'ref', 'help'])
        self.assertEqual(tabs[0].get('aria-current'), 'page')
        self.assertNotIn('id="view-home"', self.html)

    def test_css_and_javascript_cache_versions_match(self):
        versions = re.findall(r'/app/assets/app\.(?:css|js)\?v=([^"\s]+)', self.html)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0], versions[1])
        self.assertNotEqual(versions[0], '20261003-referral')

    def test_zoom_allowed_and_status_announced(self):
        viewport = next(a['content'] for t, a in self.document.tags if t == 'meta' and a.get('name') == 'viewport')
        self.assertNotIn('user-scalable=no', viewport)
        self.assertNotIn('maximum-scale=1', viewport)
        self.assertNotIn('event.touches.length > 1', self.js)
        self.assertNotIn('gesturestart', self.js)
        self.assertTrue(any(a.get('role') == 'status' or a.get('aria-live') == 'polite' for _, a in self.document.tags))

    def test_minimal_surfaces_and_visible_keyboard_focus(self):
        self.assertNotRegex(self.css, r'(?:radial|linear)-gradient\(')
        self.assertIn(':focus-visible', self.css)
        self.assertNotRegex(self.css, r'font-size:\s*(?:[0-9]|1[01])px\b')

    def test_existing_integration_paths_retained(self):
        for path in ['/miniapp/api/payments/subscription', '/miniapp/api/payments/traffic', '/miniapp/api/payments/devices', '/app/open-happ?url=']:
            self.assertIn(path, self.js)
        self.assertIn('earn_spend', self.js)
        self.assertIn('earn_withdraw', self.js)
        self.assertIn('https://t.me/share/url?', self.js)

    def test_catalog_does_not_guess_old_quota(self):
        self.assertNotRegex(self.js, r'base_gb\s*\|\|\s*200')
        self.assertNotIn('200 ГБ', self.js)

    def test_browser_suite_never_contacts_real_services_or_writes_repo_previews(self):
        source = (ROOT / 'tests/miniapp_minimal_smoke.mjs').read_text()
        self.assertIn("context.route('**/*'", source)
        self.assertIn('/private/tmp/spn-miniapp-minimal', source)
        self.assertNotIn("root + '/artifacts", source)
        self.assertNotIn('process.env.DATABASE_URL', source)

    @unittest.skipUnless(os.environ.get('MINIAPP_BROWSER_CHECK') == '1', 'Set MINIAPP_BROWSER_CHECK=1 for offline mobile Chrome checks')
    def test_mobile_browser_smoke(self):
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node required for browser checks')
        result = subprocess.run([node, 'tests/miniapp_minimal_smoke.mjs'], cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('MiniApp offline smoke passed:', result.stdout)


if __name__ == '__main__':
    unittest.main()
