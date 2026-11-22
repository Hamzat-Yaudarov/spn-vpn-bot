"""Offline checks only: do not install or contact any server."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import unittest

HERE = Path(__file__).resolve().parent


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.script = (HERE / 'spn-direct-setup.sh').read_text()
        self.profile = json.loads((HERE / 'profile.json').read_text())

    def test_shell_syntax(self):
        subprocess.run(['bash', '-n', str(HERE / 'spn-direct-setup.sh')], check=True)

    def test_matches_deployed_transport_template(self):
        spec = importlib.util.spec_from_file_location('working', HERE.parent / 'direct-ch121/manage.py')
        working = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(working)
        expected = working.config()
        for inbound in expected['inbounds']:
            inbound['tag'] = 'direct-' + inbound['streamSettings']['network']
        self.assertEqual(self.profile, expected)

    def test_no_plaintext_public_listener(self):
        for inbound in self.profile['inbounds']:
            self.assertEqual(inbound['listen'], '127.0.0.1')
            self.assertEqual(inbound['settings']['clients'], [])
            self.assertEqual(inbound['streamSettings']['security'], 'none')
        self.assertIn('ufw allow from "$PANEL_IP" to any port 2222 proto tcp', self.script)
        self.assertNotIn('ufw allow 2222', self.script)

    def test_nginx_variable_expansion_and_ports(self):
        blocks = re.findall(r'<<NGINX\n(.*?)\nNGINX', self.script, re.S)
        self.assertEqual(len(blocks), 2)
        for raw in blocks:
            shell = 'DOMAIN=node.example.com\ncat <<NGINX\n' + raw + '\nNGINX\n'
            value = subprocess.check_output(['bash', '-eu', '-c', shell], text=True)
            self.assertNotIn('$DOMAIN', value)
            self.assertNotIn('\\$', value)
            self.assertIn('server_name node.example.com;', value)
            self.assertIn('try_files $uri =404;', value)
        self.assertIn('grpc_pass grpc://127.0.0.1:10087;', value)
        self.assertIn('location ^~ /spngrpc/', value)
        self.assertIn('proxy_pass http://127.0.0.1:10088;', value)
        self.assertIn('location ^~ /direct/ws/', value)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', value)
        self.assertIn('ssl_certificate /etc/letsencrypt/live/node.example.com/fullchain.pem;', value)

    def test_ssh_connection_may_be_absent(self):
        shell = 'set -eu\n' + '\n'.join(line for line in self.script.splitlines() if line.startswith('SSH_PORT=')) + '\nprintf "%s" "$SSH_PORT"'
        self.assertEqual(subprocess.check_output(['env', '-u', 'SSH_CONNECTION', 'bash', '-c', shell], text=True), '22')

    def test_guarded_install_and_pinned_image(self):
        self.assertIn('[ ! -e /opt/remnanode ]', self.script)
        self.assertIn('--agree-le-tos', self.script)
        self.assertIn('read -r -s -p', self.script)
        self.assertRegex(self.script, r'image: remnawave/node:3\.4\.1@sha256:[0-9a-f]{64}')
        for unsafe in ('ufw reset', 'docker system prune', 'get.docker.com |'):
            self.assertNotIn(unsafe, self.script)


if __name__ == '__main__':
    unittest.main()
