"""Strict pure CDN templates, source sanitizing and role-secret regression tests."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('yandex_templates_under_test', HERE / 'config.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class API:
    def __init__(self):
        self.calls = []
        self.data = {
            'hosts': [{'uuid': c.SOURCE_HOST, 'address': 'de11cdn.wayspn.com', 'sni': 'de11cdn.wayspn.com',
                       'host': 'de11cdn.wayspn.com', 'port': 443, 'path': c.SOURCE_PATH, 'securityLayer': 'TLS',
                       'alpn': 'h2', 'fingerprint': 'firefox', 'overrideSniFromAddress': False, 'keepSniBlank': False,
                       'xhttpExtraParams': copy.deepcopy(c.CLIENT),
                       'inbound': {'configProfileUuid': c.SOURCE_PROFILE, 'configProfileInboundUuid': c.SOURCE_INBOUND}}],
            'config-profiles': [{'uuid': c.SOURCE_PROFILE, 'name': 'SOURCE',
                                 'inbounds': [{'uuid': c.SOURCE_INBOUND, 'tag': c.SOURCE_TAG}],
                                 'config': {'privateOtherProfileField': 'DO-NOT-RETURN',
                                            'inbounds': [{'tag': c.SOURCE_TAG, 'protocol': 'vless', 'listen': '127.0.0.1',
                                                          'port': 10085, 'settings': {'clients': [], 'decryption': 'none'},
                                                          'streamSettings': {'network': 'xhttp', 'security': 'none',
                                                                             'xhttpSettings': copy.deepcopy(c.SERVER)}},
                                                         {'tag': 'unrelated', 'privateKey': 'DO-NOT-RETURN'}]}}],
        }

    def rows(self, name):
        self.calls.append(name)
        if name not in self.data:
            raise AssertionError('Forbidden API query')
        return self.data[name]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.source = c.extract_source(self.api)
        self.plan = {'format': 1, 'mode': 'yandex-cdn', 'id': 'a' * 32, 'code': 'a1b2c3',
                     'ip': '8.8.4.4', 'sshPort': 22, 'panel': 'https://panel.example.com/api', 'panelIp': '1.1.1.1',
                     'name': '🇩🇪 Германия', 'country': 'DE', 'domain': 'origin.example.com',
                     'cdnDomain': 'cdn.example.com', 'dns': {'provider': 'cloudflare', 'zone': 'example.com', 'ttl': 60},
                     'path': '/assets/edge-0123456789abcdef/', 'squads': ['11111111-1111-4111-8111-111111111111']}

    def test_valid_plan_and_templates_do_not_mutate_inputs(self):
        before = copy.deepcopy((self.plan, self.source))
        self.assertEqual(c.validate_plan(self.plan), self.plan)
        c.profile(self.plan, self.source)
        c.role_files(self.plan, 'NODE-SECRET-ONLY-THIS-ROLE-12345')
        self.assertEqual((self.plan, self.source), before)

    def test_plan_rejects_unexpected_fields_or_invalid_types(self):
        mutations = [('format', True), ('mode', 'direct'), ('code', 'abc;id'), ('id', '../oops'), ('sshPort', True),
                     ('ip', '127.0.0.1'), ('panelIp', '8.8.4.4'), ('country', 'de'), ('name', 'a\nline'),
                     ('path', '/assets/edge-id/'), ('squads', []), ('squads', [[]]),
                     ('squads', ['11111111-1111-4111-8111-111111111111'] * 2)]
        for key, value in mutations:
            with self.subTest(key=key), self.assertRaises(c.ConfigError):
                c.validate_plan({**self.plan, key: value})
        with self.assertRaises(c.ConfigError):
            c.validate_plan({**self.plan, 'extra': 'not allowed'})

    def test_reserved_ssh_ports_and_bad_panel_urls_are_rejected(self):
        for port in (0, 80, 443, 2222, 10050, 10085, 65536, '22'):
            with self.subTest(port=port), self.assertRaises(c.ConfigError):
                c.validate_plan({**self.plan, 'sshPort': port})
        for url in ('http://panel.example/api', 'https://user:pass@panel.example/api',
                    'https://panel.example/api?secret=bad', 'https://panel.example/admin', 123):
            with self.subTest(url=url), self.assertRaises(c.ConfigError):
                c.validate_plan({**self.plan, 'panel': url})

    def test_domains_are_distinct_normalized_subdomains_without_injection(self):
        for key, value in [('domain', 'cdn.example.com'), ('domain', 'example.com'),
                           ('domain', 'outside.test'), ('cdnDomain', 'CDN.example.com'),
                           ('cdnDomain', 'origin.example.com'), ('domain', '8.8.8.8'),
                           ('domain', 'origin.example.com; return 200;')]:
            with self.subTest(value=value), self.assertRaises(c.ConfigError):
                c.validate_plan({**self.plan, key: value})

    def test_source_extraction_gets_infrastructure_only_and_sanitizes(self):
        self.assertEqual(self.api.calls, ['hosts', 'config-profiles'])
        self.assertEqual(set(self.source), {'server', 'client', 'fingerprint', 'extraEncoding', 'sourceHost'})
        self.assertNotIn('DO-NOT-RETURN', json.dumps(self.source))
        self.assertNotIn('clients', json.dumps(self.source))
        self.assertEqual(self.source['server'], c.SERVER)
        self.assertEqual(self.source['client'], c.CLIENT)

    def test_string_client_encoding_is_preserved(self):
        self.api.data['hosts'][0]['xhttpExtraParams'] = json.dumps(c.CLIENT)
        source = c.extract_source(self.api)
        self.assertEqual(source['extraEncoding'], 'json-string')
        uid = '11111111-1111-4111-8111-111111111111'
        host = c.host_body(self.plan, source, uid, uid, uid)
        self.assertIsInstance(host['xhttpExtraParams'], str)
        self.assertEqual(json.loads(host['xhttpExtraParams']), c.CLIENT)

    def test_source_duplicate_json_keys_are_rejected_without_echoing_values(self):
        self.api.data['hosts'][0]['xhttpExtraParams'] = '{"secret":"DO-NOT-ECHO","secret":"again"}'
        with self.assertRaises(c.ConfigError) as error:
            c.extract_source(self.api)
        self.assertNotIn('DO-NOT-ECHO', str(error.exception))

    def test_source_extra_settings_and_overrides_cannot_be_adopted(self):
        mutations = [lambda a: a.data['hosts'][0]['xhttpExtraParams'].update(path='/different/'),
                     lambda a: a.data['hosts'][0]['xhttpExtraParams'].update(downloadSettings={'address': 'elsewhere'}),
                     lambda a: a.data['hosts'][0]['xhttpExtraParams'].update(uplinkDataPlacement='body'),
                     lambda a: a.data['hosts'][0]['xhttpExtraParams'].update(uplinkHTTPMethod='POST'),
                     lambda a: a.data['hosts'][0].update(sni='different.example.com'),
                     lambda a: a.data['hosts'][0].update(fingerprint='safari'),
                     lambda a: a.data['config-profiles'][0]['config']['inbounds'][0]['settings'].update(clients=[{'id': 'DO-NOT-READ'}]),
                     lambda a: a.data['config-profiles'][0]['config']['inbounds'][0]['streamSettings']['xhttpSettings'].update(serverMaxHeaderBytes=8192.0),
                     lambda a: a.data['config-profiles'][0]['config']['inbounds'][0]['streamSettings']['xhttpSettings'].update(scMaxEachPostBytes='4096')]
        for change in mutations:
            api = API(); change(api)
            with self.subTest(change=change), self.assertRaises(c.ConfigError):
                c.extract_source(api)

    def test_source_binding_and_ambiguity_fail_closed(self):
        for action in (lambda a: a.data['hosts'].append(copy.deepcopy(a.data['hosts'][0])),
                       lambda a: a.data['hosts'][0]['inbound'].update(configProfileUuid='other'),
                       lambda a: a.data['config-profiles'][0]['inbounds'][0].update(tag='wrong')):
            api = API(); action(api)
            with self.assertRaises(c.ConfigError):
                c.extract_source(api)

    def test_profile_is_single_loopback_inbound_with_own_path(self):
        profile = c.profile(self.plan, self.source)
        self.assertEqual(len(profile['inbounds']), 1)
        inbound = profile['inbounds'][0]
        self.assertEqual((inbound['listen'], inbound['port'], inbound['protocol']), ('127.0.0.1', 10085, 'vless'))
        self.assertEqual(inbound['tag'], 'yandex-xhttp-a1b2c3')
        self.assertEqual(inbound['settings'], {'clients': [], 'decryption': 'none'})
        self.assertEqual(inbound['streamSettings']['security'], 'none')
        server = inbound['streamSettings']['xhttpSettings']
        self.assertEqual(server['path'], self.plan['path'])
        self.assertEqual(server['mode'], 'packet-up')
        self.assertEqual(server['scMaxEachPostBytes'], '2048')
        self.assertEqual(server['uplinkHTTPMethod'], 'GET')
        self.assertEqual(server['uplinkDataPlacement'], 'header')
        self.assertNotIn('privateKey', json.dumps(profile))
        self.assertIn('224.0.0.0/4', profile['routing']['rules'][0]['ip'])

    def test_host_is_disabled_tls_custom_edge_not_origin(self):
        ids = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222',
               '33333333-3333-4333-8333-333333333333']
        host = c.host_body(self.plan, self.source, *ids)
        self.assertTrue(host['isDisabled'])
        self.assertEqual(host['address'], self.plan['cdnDomain'])
        self.assertEqual(host['sni'], host['address'])
        self.assertEqual(host['host'], host['address'])
        self.assertEqual(host['fingerprint'], 'firefox')
        self.assertEqual(host['alpn'], 'h2')
        self.assertEqual(host['securityLayer'], 'TLS')
        self.assertEqual(host['path'], self.plan['path'])
        self.assertEqual(host['nodes'], [ids[1]])
        self.assertEqual(host['xhttpExtraParams'], c.CLIENT)
        self.assertNotIn(self.plan['ip'], json.dumps(host))
        self.assertNotIn(self.plan['domain'], json.dumps(host))

    def test_nginx_matches_origin_path_and_prevents_public_core_bypass(self):
        files = c.role_files(self.plan, 'NODE-SECRET-ONLY-THIS-ROLE-12345')
        nginx = files['nginx.conf'].decode()
        self.assertIn('listen 8.8.4.4:443 ssl http2;', nginx)
        self.assertIn('server_name origin.example.com;', nginx)
        self.assertIn('ssl_reject_handshake on;', nginx)
        self.assertIn('location ^~ ' + self.plan['path'] + ' {', nginx)
        self.assertIn('proxy_pass http://127.0.0.1:10085;', nginx)
        for item in ('proxy_request_buffering off;', 'proxy_buffering off;', 'proxy_cache off;',
                     'proxy_set_header Host origin.example.com;', 'proxy_set_header Connection "";',
                     'proxy_set_header Forwarded "";', 'large_client_header_buffers 4 8k;',
                     'proxy_read_timeout 3600s;', 'add_header Cache-Control no-store always;', 'access_log off;'):
            self.assertIn(item, nginx)
        self.assertNotIn('listen 10085', nginx)
        self.assertNotIn(self.plan['cdnDomain'], nginx)

    def test_health_is_deployment_specific_and_bootstrap_is_not_false_success(self):
        files = c.role_files(self.plan, 'NODE-SECRET-ONLY-THIS-ROLE-12345')
        self.assertIn(b'"status":"ok","deployment":"a1b2c3"', files['nginx.conf'])
        self.assertIn(b'return 503', files['bootstrap.conf'])
        self.assertNotIn(b'"status":"ok"', files['bootstrap.conf'])
        self.assertIn(b'root /var/www/spn-yandex-acme;', files['bootstrap.conf'])

    def test_templates_have_only_the_node_secret_and_pinned_nonprivileged_container(self):
        secret = 'NODE-SECRET-ONLY-THIS-ROLE-12345'
        files = c.role_files(self.plan, secret)
        self.assertEqual(files['node.env'], ('NODE_PORT=2222\nSECRET_KEY=' + secret + '\n').encode())
        self.assertEqual(files['.spn-deployment'], ('a' * 32 + '\n').encode())
        for name, content in files.items():
            self.assertIsInstance(content, bytes)
            self.assertEqual(secret.encode() in content, name == 'node.env')
        compose = files['docker-compose.yml'].decode()
        self.assertIn(c.IMAGE, compose)
        self.assertIn('network_mode: host', compose)
        self.assertIn('spn.deploy.mode: "yandex-cdn"', compose)
        self.assertNotIn('privileged:', compose)
        self.assertNotIn('volumes:', compose)
        self.assertIn('worker_connections 8192', files['nginx-main.conf'].decode())
        self.assertIn('worker_rlimit_nofile 65535', files['nginx-main.conf'].decode())

    def test_secrets_cannot_inject_new_environment_entries(self):
        for secret in ('short', 'A' * 30 + '\nANOTHER=secret', 'A' * 30 + '\x00', 'A' * 8193):
            with self.subTest(secret_length=len(secret)), self.assertRaises(c.ConfigError):
                c.role_files(self.plan, secret)

    def test_renew_hook_validates_before_reload_and_has_valid_shell_syntax(self):
        hook = c.role_files(self.plan, 'NODE-SECRET-ONLY-THIS-ROLE-12345')['renew-nginx.sh']
        self.assertLess(hook.index(b'nginx -t'), hook.index(b'systemctl reload'))
        result = subprocess.run(['sh', '-n'], input=hook, capture_output=True)
        self.assertEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
