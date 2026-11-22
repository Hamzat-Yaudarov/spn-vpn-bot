#!/usr/bin/env python3
"""Publish the password-protected loopback reserve panel through owned HTTPS nginx."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile


OWNER = '# spn-reserve-fleet-https-v1'
SITE = Path('/etc/nginx/sites-available/spn-reserve-fleet.conf')
ENABLED = Path('/etc/nginx/sites-enabled/spn-reserve-fleet.conf')
WEBROOT = Path('/var/www/spn-reserve-acme')
HOOK = Path('/etc/letsencrypt/renewal-hooks/deploy/spn-reserve-fleet')
PASSWORD = Path('/var/lib/spn-reserve-fleet/.spn-reserve-fleet/password.json')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def domain_name(value):
    require(isinstance(value, str) and 3 <= len(value) <= 253 and value == value.lower()
            and value.isascii() and not re.fullmatch('[0-9.]+', value), 'A lowercase DNS domain is required')
    labels = value.split('.')
    require(len(labels) >= 2 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                   for label in labels), 'Invalid DNS domain')
    return value


def site_text(domain, *, tls):
    domain = domain_name(domain)
    http = f'''{OWNER}
# domain: {domain}
server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    location ^~ /.well-known/acme-challenge/ {{
        root {WEBROOT};
        default_type text/plain;
        try_files $uri =404;
    }}
    location / {{ {'return 301 https://' + domain + '$request_uri;' if tls else 'return 404;'} }}
}}
'''
    if not tls:
        return http
    return http + f'''
server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};
    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 64k;
    location / {{
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Connection "";
        proxy_connect_timeout 10s;
        proxy_read_timeout 1000s;
        proxy_send_timeout 1000s;
        proxy_buffering off;
    }}
}}
'''


def hook_text(domain):
    domain = domain_name(domain)
    return f'''#!/bin/sh
{OWNER}
set -eu
[ "${{RENEWED_LINEAGE:-}}" = "/etc/letsencrypt/live/{domain}" ] || exit 0
/usr/sbin/nginx -t
/bin/systemctl reload nginx
'''


def owned_file(path, allowed):
    if not path.exists() and not path.is_symlink():
        return None
    require(not path.is_symlink() and path.is_file(), 'Unsafe managed file: ' + path.name)
    info = path.stat()
    require(info.st_uid == 0 and info.st_nlink == 1 and not info.st_mode & 0o022,
            'Managed file must be root-owned without writable group/other permissions: ' + path.name)
    raw = path.read_text()
    require(raw in allowed, 'Existing file is not this unchanged HTTPS installation: ' + path.name)
    return raw


def safe_directory(path):
    require(not any(part.is_symlink() for part in (path, *path.parents)), 'Unsafe HTTPS directory')
    path.mkdir(parents=True, exist_ok=True, mode=0o755)
    info = path.stat()
    require(info.st_uid == 0 and not info.st_mode & 0o022, 'HTTPS directory must be root-owned')


def write_atomic(path, value, mode=0o644):
    descriptor, temporary = tempfile.mkstemp(prefix='.spn-https-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), mode)
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(command, *, capture=False):
    result = subprocess.run(command, check=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive', 'LC_ALL': 'C'})
    return result.stdout if capture else None


def validate_certificate(domain):
    lineage = Path('/etc/letsencrypt/live') / domain
    certificate, key = lineage / 'fullchain.pem', lineage / 'privkey.pem'
    require(certificate.is_file() and key.is_file(), 'TLS certificate/key is missing')
    key_info = key.stat()
    require(key_info.st_uid == 0 and not key_info.st_mode & 0o077, 'TLS key permissions are unsafe')
    run(['openssl', 'verify', '-CApath', '/etc/ssl/certs', '-untrusted', str(certificate),
         '-purpose', 'sslserver', '-verify_hostname', domain, str(certificate)], capture=True)
    fresh = run(['openssl', 'x509', '-in', str(certificate), '-noout', '-checkend', '86400'], capture=True)
    require(fresh.strip() == b'Certificate will not expire', 'TLS certificate expires within one day')
    public = run(['openssl', 'x509', '-in', str(certificate), '-noout', '-pubkey'], capture=True)
    actual = run(['openssl', 'pkey', '-in', str(key), '-pubout'], capture=True)
    require(public == actual, 'TLS private key does not match certificate')


def setup(args):
    domain = domain_name(args.domain)
    plan = {'planOnly': not args.apply, 'domain': domain, 'url': 'https://' + domain,
            'upstream': '127.0.0.1:8787', 'certificate': 'Let\u2019s Encrypt HTTP-01',
            'site': str(SITE), 'webroot': str(WEBROOT), 'changesVpnNodes': False}
    if not args.apply:
        return plan
    require(sys.platform.startswith('linux') and os.geteuid() == 0, 'HTTPS apply requires Linux root')
    account = pwd.getpwnam('spn-reserve')
    require(PASSWORD.is_file() and not PASSWORD.is_symlink(), 'Set the panel password before publishing HTTPS')
    info = PASSWORD.stat()
    require(info.st_uid == account.pw_uid and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
            and info.st_size > 0, 'Panel password ownership/mode is unsafe')
    http, https = site_text(domain, tls=False), site_text(domain, tls=True)
    previous = owned_file(SITE, (http, https))
    owned_file(HOOK, (hook_text(domain),))
    if ENABLED.exists() or ENABLED.is_symlink():
        require(ENABLED.is_symlink() and os.readlink(ENABLED) == str(SITE), 'Existing nginx site link is unrelated')
    lineage = Path('/etc/letsencrypt/live') / domain
    require(previous is not None or not lineage.exists(), 'An unrelated certificate already exists for this domain')
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=600', 'update'])
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=600', 'install', '-y', 'nginx', 'certbot', 'openssl'])
    for path in (SITE.parent, ENABLED.parent, WEBROOT, HOOK.parent):
        safe_directory(path)
    # Keep HTTPS available on repeat runs; initial issuance exposes only ACME,
    # never the password form over cleartext HTTP.
    if previous != https:
        write_atomic(SITE, http)
    if not ENABLED.is_symlink():
        ENABLED.symlink_to(SITE)
    run(['nginx', '-t'])
    run(['systemctl', 'enable', '--now', 'nginx'])
    run(['systemctl', 'reload', 'nginx'])
    run(['certbot', 'certonly', '--webroot', '-w', str(WEBROOT), '--preferred-challenges', 'http',
         '--non-interactive', '--agree-tos', '--register-unsafely-without-email', '--keep-until-expiring',
         '--cert-name', domain, '-d', domain])
    validate_certificate(domain)
    write_atomic(HOOK, hook_text(domain), mode=0o700)
    write_atomic(SITE, https)
    run(['nginx', '-t'])
    run(['systemctl', 'reload', 'nginx'])
    run(['systemctl', 'enable', '--now', 'certbot.timer'])
    run(['systemctl', 'is-active', '--quiet', 'nginx'])
    run(['systemctl', 'is-active', '--quiet', 'certbot.timer'])
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(setup(args), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print('HTTPS setup stopped: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
