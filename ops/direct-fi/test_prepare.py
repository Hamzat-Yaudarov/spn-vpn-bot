"""Offline deployment-template checks; never execute installation or network I/O."""

from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent
HOST = "fi103.wayspn.com"
IP = "92.42.102.103"
PANEL = "144.31.170.147"
IMAGE = "remnawave/node:3.4.1@sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e"
SCRIPTS = ("install-files.sh", "finish.sh", "renew-nginx.sh")


def source(name):
    return (ROOT / name).read_text(encoding="utf-8")


def commands(name):
    return "\n".join(line for line in source(name).splitlines() if not line.lstrip().startswith("#"))


class FinlandDeploymentTemplateTests(unittest.TestCase):
    def test_shell_syntax_only(self):
        for name in SCRIPTS:
            with self.subTest(script=name):
                result = subprocess.run(
                    ["/bin/bash", "-n", str(ROOT / name)],
                    stdin=subprocess.DEVNULL, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_scripts_pin_authorized_target_and_require_apply(self):
        for name in ("install-files.sh", "finish.sh"):
            text = commands(name)
            with self.subTest(script=name):
                self.assertIn("stage=/root/direct-fi", text)
                self.assertIn("--apply", text)
                self.assertIn("root required", text)
                self.assertIn("ip -o -4 addr show scope global", text)
                self.assertIn("grep -Fxq " + IP, text)
                self.assertIn("wrong VPS address", text)
                self.assertIn("0:700", text)
                self.assertIn("stat -c '%u:%h'", text)
                self.assertIn("[ ! -L", text)
                self.assertIn("8#$mode & 022", text)
                self.assertNotIn("64.188.78.29", text)
                self.assertNotIn("node29.prestizh.site", text)

    def test_installer_refuses_existing_destinations_and_never_starts_node(self):
        text = commands("install-files.sh")
        self.assertIn("target already exists; inspect before retry", text)
        self.assertIn("/opt/remnanode", text)
        self.assertIn("/var/www/direct-fi-acme", text)
        self.assertIn("unexpected enabled Nginx site", text)
        self.assertIn("container name already exists", text)
        self.assertIn("mv -- /etc/nginx/sites-enabled/default", text)
        self.assertIn("trap rollback EXIT", text)
        self.assertIn("nginx -t", text)
        self.assertNotRegex(text, r"docker\s+(?:compose[^\n]*\s+up|start|restart|run|pull)\b")

    def test_bootstrap_is_only_exact_host_acme_and_unavailable_page(self):
        text = commands("bootstrap.conf")
        self.assertIn("server_name " + HOST + ";", text)
        self.assertIn("listen 80 default_server;", text)
        self.assertIn("return 444;", text)
        self.assertIn("location ^~ /.well-known/acme-challenge/", text)
        self.assertIn("root /var/www/direct-fi-acme;", text)
        self.assertIn("try_files $uri =404;", text)
        self.assertIn("location / { return 503; }", text)
        self.assertNotIn("proxy_pass", text)
        self.assertNotIn("listen 443", text)

    def test_tls_ws_proxy_is_exact_finland_host_and_loopback_only(self):
        text = commands("nginx.conf")
        self.assertIn("ssl_reject_handshake on;", text)
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", text)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/" + HOST + "/fullchain.pem;", text)
        self.assertIn("ssl_certificate_key /etc/letsencrypt/live/" + HOST + "/privkey.pem;", text)
        self.assertIn("if ($host != " + HOST + ") { return 444; }", text)
        self.assertIn("if ($ssl_server_name != " + HOST + ") { return 444; }", text)
        self.assertIn("location ^~ /direct/ws/", text)
        self.assertEqual(re.findall(r"proxy_pass\s+([^;]+);", text), ["http://127.0.0.1:10088"])
        self.assertIn("proxy_http_version 1.1;", text)
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", text)
        self.assertIn('proxy_set_header Connection "upgrade";', text)
        self.assertIn("proxy_set_header Host " + HOST + ";", text)
        self.assertIn("proxy_cache off;", text)
        self.assertIn("proxy_buffering off;", text)
        self.assertIn("add_header Cache-Control no-store always;", text)
        self.assertNotIn("$proxy_add_x_forwarded_for", text)

    def test_grpc_is_not_misconfigured_as_nginx_tls_proxy(self):
        text = commands("nginx.conf")
        self.assertNotIn("grpc_pass", text)
        self.assertNotRegex(text, r"\blisten\s+[^;]*\b2083\b")
        self.assertNotIn("10087", text)
        self.assertNotIn("direct-grpc", text)
        self.assertNotIn("10086", text)

    def test_node_image_is_digest_pinned_with_no_tls_secret_volume(self):
        text = commands("docker-compose.yml")
        self.assertIn("image: " + IMAGE, text)
        self.assertIn("network_mode: host", text)
        self.assertIn("container_name: remnanode", text)
        self.assertIn("env_file:", text)
        self.assertIn("- node.env", text)
        self.assertIn("max-size: 20m", text)
        self.assertNotIn("volumes:", text)
        self.assertNotIn("privkey.pem", text)
        self.assertNotIn("latest", text)

    def test_node_start_is_explicit_and_declares_panel_only_api_guard(self):
        text = commands("finish.sh")
        self.assertIn("start_node=0", text)
        self.assertIn("--apply --start-node --confirm-panel-only " + PANEL, text)
        self.assertIn('if [ "$start_node" = 1 ]; then', text)
        self.assertIn("docker image inspect " + IMAGE, text)
        self.assertIn("pinned image must be pulled separately", text)
        self.assertIn("up -d --pull never --no-build", text)
        # Host-network port protection is a separately verified prerequisite,
        # not an implicit permission for these scripts to alter the firewall.
        self.assertIn("TCP2222 must already be reachable ONLY from panel" + PANEL, source("finish.sh"))
        self.assertIn("TCP22/80/443/2083", source("finish.sh"))

    def test_no_automatic_firewall_mutation_extra_transport_or_cert_issuance(self):
        for name in SCRIPTS:
            text = commands(name)
            with self.subTest(script=name):
                self.assertNotRegex(text, r"\b(?:ufw|iptables|ip6tables|nft|firewall-cmd)\b")
                self.assertNotRegex(text, r"\b(?:certbot|acme\.sh)\b")
                self.assertNotIn("--agree-tos", text)
                self.assertNotIn("--register-unsafely-without-email", text)
                self.assertNotRegex(text, r"\b(?:apt-get|curl|wget)\b")
                self.assertNotRegex(text, re.compile(r"\b8443\b|\bhysteria\b", re.IGNORECASE))
        for name in ("nginx.conf", "bootstrap.conf", "docker-compose.yml"):
            self.assertNotRegex(commands(name), re.compile(r"\b8443\b|\budp\b|\bhysteria\b", re.IGNORECASE))

    def test_finish_checks_existing_certificate_and_can_restore_bootstrap(self):
        text = commands("finish.sh")
        self.assertIn("site is not the expected bootstrap", text)
        self.assertIn("HTTPS target already exists; inspect before retry", text)
        self.assertIn("openssl x509", text)
        self.assertIn("-checkend 86400", text)
        self.assertIn("-ext subjectAltName", text)
        self.assertIn("DNS:" + HOST, text)
        self.assertIn("openssl verify -CApath /etc/ssl/certs", text)
        self.assertIn('-untrusted "$cert/fullchain.pem"', text)
        self.assertIn("served certificate leaf differs", text)
        self.assertIn('-in "$cert/fullchain.pem" -noout -fingerprint -sha256', text)
        self.assertIn("trap rollback EXIT", text)
        self.assertIn('cp -- "$backup" "$site"', text)
        self.assertIn("nginx -t", text)
        self.assertLess(text.index("openssl verify"), text.index('install -m 0644 "$stage/nginx.conf"'))
        self.assertLess(text.index('install -m 0644 "$stage/nginx.conf"'), text.index('install -m 0755 "$stage/renew-nginx.sh"'))

    def test_renewal_is_exact_lineage_nginx_only(self):
        text = commands("renew-nginx.sh")
        self.assertIn('"${RENEWED_LINEAGE:-}" != /etc/letsencrypt/live/' + HOST, text)
        self.assertIn("exit 0", text)
        self.assertIn("nginx -t", text)
        self.assertIn("systemctl reload nginx", text)
        self.assertNotIn("docker", text)
        self.assertNotIn("restart", text)


if __name__ == "__main__":
    unittest.main()
