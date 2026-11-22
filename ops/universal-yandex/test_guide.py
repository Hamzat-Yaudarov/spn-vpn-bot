"""Manual CDN guide checks; no browser, network, DNS, files or purchases."""
import copy
import importlib.util
from pathlib import Path
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('yandex_guide_tests', HERE / 'guide.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'domain': 'origin-a1b2c3.wayspn.com', 'cdnDomain': 'cdn-a1b2c3.wayspn.com',
                     'ip': '8.8.8.8', 'code': 'a1b2c3', 'path': '/assets/a1b2c3/',
                     'name': '🇩🇪 Германия', 'dns': {'zone': 'wayspn.com'}}
        self.command = "python3 '/Users/owner/My Project/ops/universal-yandex/deploy.py' --ip 8.8.8.8"

    def render(self):
        return g.render(self.plan, self.command)

    def test_every_installation_value_is_concrete_and_roles_are_separate(self):
        text = self.render()
        for value in (self.plan['domain'], self.plan['cdnDomain'], self.plan['ip'], self.plan['code'],
                      self.plan['path'], self.plan['name'], self.plan['dns']['zone']):
            self.assertIn(value, text)
        self.assertIn('_acme-challenge.' + self.plan['cdnDomain'], text)
        self.assertIn('SNI = `' + self.plan['domain'] + '`', text)
        self.assertIn('без `https://`', text)

    def test_three_resumable_commands_keep_parent_quoted_prefix(self):
        text = self.render()
        self.assertIn(self.command + ' --certificate-target "CERTIFICATE_CNAME_TARGET" --apply', text)
        self.assertIn(self.command + ' --cdn-target "CDN_CNAME_TARGET" --apply', text)
        self.assertIn(self.command + ' --finish --apply', text)
        self.assertIn('Не запускай команду с буквальным текстом-заглушкой', text)
        self.assertIn('буквальную заглушку запускать нельзя', text)

    def test_certificate_precedes_resource_and_distribution_cname(self):
        text = self.render()
        self.assertLess(text.index('## 2.'), text.index('## 3.'))
        self.assertLess(text.index('--certificate-target'), text.index('--cdn-target'))
        self.assertIn('одном каталоге', text)
        self.assertIn('не удаляй запись после выпуска', text)
        self.assertIn('Issued', text)

    def test_header_transport_does_not_need_post_or_get_body(self):
        text = self.render()
        for value in ('GET, HEAD, OPTIONS', 'GET/header', 'без тела GET-запроса', '413',
                      'X-Transport-Data', 'Не удалять query-параметры'):
            self.assertIn(value, text)
        self.assertNotIn('Разрешить POST', text)

    def test_no_guaranteed_whitelist_bypass_or_invented_http2_button(self):
        text = self.render()
        self.assertIn('не дают гарантии обхода', text)
        self.assertIn('Отдельный переключатель HTTP/2 искать не нужно', text)
        self.assertNotIn('Включи HTTP/2', text)
        self.assertIn('настоящий VPN-трафик', text)

    def test_payment_is_operator_only_and_not_a_fixed_150_promise(self):
        text = self.render()
        self.assertIn('Скрипт ничего не покупает', text)
        self.assertIn('Нажми платное подтверждение сам', text)
        self.assertIn('сверх пакета', text)
        self.assertNotIn('150 ₽', text)
        self.assertNotIn('150 рублей', text)

    def test_official_sources_are_present_inline(self):
        text = self.render()
        for url in g.SOURCES.values():
            self.assertIn('](' + url + ')', text)

    def test_names_are_markdown_escaped_without_command_interpolation(self):
        self.plan['name'] = 'Project | [unsafe](https://example.org) `name`'
        text = self.render()
        self.assertIn(r'Project \| \[unsafe\]', text)
        self.assertIn(self.command + ' --finish --apply', text)

    def test_rejects_invalid_values_or_multiline_command(self):
        for key, value in (('domain', self.plan['cdnDomain']), ('cdnDomain', 'cdn.foreign.example'),
                           ('ip', '127.0.0.1'), ('path', '/assets/\n'), ('code', 'x; id'),
                           ('name', 'x\nDo something')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                plan = copy.deepcopy(self.plan); plan[key] = value
                g.render(plan, self.command)
        with self.assertRaises(ValueError):
            g.render(self.plan, 'python3 tool.py\necho unsafe')


if __name__ == '__main__':
    unittest.main()
