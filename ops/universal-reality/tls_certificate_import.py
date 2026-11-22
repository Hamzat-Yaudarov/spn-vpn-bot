#!/usr/bin/env python3
"""Import one privately issued TLS lineage into an owned replacement guest.

Only the certificate, its private key and the operation's isolated ACME account
are accepted. DNS credentials and Certbot manual hooks are never imported.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

SERVER = 'https://acme-v02.api.letsencrypt.org/directory'
ACCOUNT_SERVER = 'acme-v02.api.letsencrypt.org/directory'
FILES = ('cert.pem', 'chain.pem', 'fullchain.pem', 'privkey.pem',
         'meta.json', 'regr.json', 'private_key.json')
MAX_BUNDLE = 256 * 1024


class ImportError(RuntimeError):
    """A fixed, public import failure; never contains certificate contents."""


def require(condition, message):
    if not condition:
        raise ImportError(message)


def _safe_parents(path):
    require(all(not parent.is_symlink() for parent in (path, *path.parents)),
            'Certificate import path contains a symbolic link')


def _directory(path, uid, *, create=False, private=True):
    _safe_parents(path)
    if create and not path.exists():
        path.mkdir(mode=0o700)
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
            and (stat.S_IMODE(info.st_mode) == 0o700 if private
                 else not stat.S_IMODE(info.st_mode) & 0o022),
            'Unsafe certificate import directory')


def _read(path, uid, *, limit=MAX_BUNDLE, modes=(0o600,)):
    _safe_parents(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) in modes and info.st_size <= limit,
                'Unsafe certificate import file')
        value = stream.read(limit + 1)
    require(len(value) <= limit, 'Certificate import file is too large')
    return value


def _write(path, data, uid):
    """Never overwrite an existing byte, including on an interrupted import."""
    _directory(path.parent, uid, private=False)
    if path.exists() or path.is_symlink():
        require(_read(path, uid) == data, 'Owned certificate import file changed')
        return
    fd, temporary = tempfile.mkstemp(prefix='.spn-tls-import-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            require(_read(path, uid) == data, 'Owned certificate import file changed')
        os.unlink(temporary)
        temporary = None
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def _json(value):
    try:
        return json.loads(value)
    except (UnicodeError, ValueError):
        raise ImportError('Invalid certificate import JSON') from None


def validate_bundle(value, domain):
    require(isinstance(domain, str) and re.fullmatch(
        r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+', domain)
        and len(domain) <= 253, 'Invalid certificate import domain')
    require(isinstance(value, dict) and set(value) == {'format', 'domain', 'account_id', 'server', 'files'}
            and value['format'] == 1 and value['domain'] == domain and value['server'] == SERVER
            and isinstance(value['account_id'], str) and re.fullmatch('[0-9a-f]{32}', value['account_id'])
            and isinstance(value['files'], dict) and set(value['files']) == set(FILES),
            'Certificate import identity differs')
    for name, contents in value['files'].items():
        require(isinstance(contents, str) and 0 < len(contents.encode('utf-8')) <= 64 * 1024
                and '\x00' not in contents, 'Invalid certificate import payload')
        if name.endswith('.json'):
            require(isinstance(_json(contents), dict), 'Invalid ACME account JSON')
    account = _json(value['files']['private_key.json'])
    require(account.get('kty') in ('RSA', 'EC') and isinstance(account.get('d'), str)
            and re.fullmatch('[A-Za-z0-9_-]+', account['d']), 'Invalid ACME account private key')
    registration = _json(value['files']['regr.json'])
    require(isinstance(registration.get('body'), dict) and isinstance(registration.get('uri'), str)
            and re.fullmatch(r'https://acme-v02\.api\.letsencrypt\.org/acme/acct/[0-9]+', registration['uri']),
            'Invalid ACME account registration')
    return value


def _openssl(arguments, *, expected=None):
    try:
        result = subprocess.run(['openssl', *arguments], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise ImportError('TLS certificate validation is unavailable') from None
    require(result.returncode == 0 and (expected is None or expected in result.stdout),
            'TLS certificate validation failed')
    return result.stdout


def validate_certificate(files, domain):
    """Validate public trust, hostname, expiry and the exact private key."""
    with tempfile.TemporaryDirectory(prefix='spn-tls-import-') as temporary:
        directory = Path(temporary)
        for name in FILES[:4]:
            path = directory / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(files[name].encode())
        certificate = str(directory / 'cert.pem')
        fullchain = str(directory / 'fullchain.pem')
        _openssl(['verify', '-CApath', '/etc/ssl/certs', '-untrusted', str(directory / 'chain.pem'),
                  '-purpose', 'sslserver', '-verify_hostname', domain, certificate])
        _openssl(['x509', '-in', certificate, '-noout', '-checkend', '86400'],
                 expected=b'Certificate will not expire')
        require(_openssl(['x509', '-in', certificate, '-outform', 'DER'])
                == _openssl(['x509', '-in', fullchain, '-outform', 'DER']),
                'TLS full chain differs from its certificate')
        require(_openssl(['x509', '-in', certificate, '-noout', '-pubkey'])
                == _openssl(['pkey', '-in', str(directory / 'privkey.pem'), '-pubout']),
                'TLS private key differs from its certificate')
        require(files['fullchain.pem'].strip() == (files['cert.pem'].strip() + '\n'
                + files['chain.pem'].strip()), 'TLS full chain is incomplete')


def _renewal(root, domain, account, mode='standalone'):
    live = root / 'live' / domain
    return (f'# Managed SPN TLS replacement; DNS credentials are not stored on this VPS.\n'
            f'archive_dir = {root / "archive" / domain}\n'
            f'cert = {live / "cert.pem"}\n'
            f'privkey = {live / "privkey.pem"}\n'
            f'chain = {live / "chain.pem"}\n'
            f'fullchain = {live / "fullchain.pem"}\n\n'
            '[renewalparams]\n'
            f'account = {account}\n'
            'key_type = ecdsa\n'
            'elliptic_curve = secp256r1\n'
            f'authenticator = {"webroot" if mode == "yandex-cdn" else "standalone"}\n'
            'pref_challs = http-01,\n'
            f'server = {SERVER}\n'
            + ('webroot_path = /var/www/spn-yandex-acme,\n' if mode == 'yandex-cdn' else '')).encode()


def _audit_completed(root, node, value, marker_bytes, uid, validator, mode='standalone'):
    domain = value['domain']
    require(_read(node / 'tls-domain', uid) == (domain + '\n').encode(),
            'Certificate domain ownership changed')
    for name in ('archive', 'live'):
        directory = root / name / domain
        _directory(directory, uid)
        require(_read(directory / '.spn-tls-import', uid) == marker_bytes,
                'Certificate lineage ownership changed')
    current, versions = {}, set()
    for name in FILES[:4]:
        link = root / 'live' / domain / name
        require(link.is_symlink() and link.lstat().st_uid == uid, 'Certificate lineage link changed')
        target = os.readlink(link)
        pattern = (r'\.\./\.\./archive/' + re.escape(domain) + '/' + name.removesuffix('.pem')
                   + r'([1-9][0-9]*)\.pem')
        match = re.fullmatch(pattern, target)
        require(match is not None, 'Certificate lineage link changed')
        versions.add(match[1])
        path = root / 'archive' / domain / Path(target).name
        current[name] = _read(path, uid, modes=(0o600,) if name == 'privkey.pem'
                              else (0o600, 0o644)).decode()
    require(len(versions) == 1, 'Certificate lineage versions differ')
    account = root / 'accounts' / ACCOUNT_SERVER / value['account_id']
    for name in FILES[4:]:
        current[name] = _read(account / name, uid).decode()
    validate_bundle({**value, 'files': current}, domain)
    validator(current, domain)
    renewal = root / 'renewal' / (domain + '.conf')
    try:
        settings = configparser.ConfigParser(interpolation=None)
        settings.read_string('[lineage]\n' + _read(renewal, uid, modes=(0o600, 0o644)).decode())
    except (configparser.Error, UnicodeError):
        raise ImportError('Certificate renewal configuration changed') from None
    expected = {'archive_dir': str(root / 'archive' / domain),
                **{name.removesuffix('.pem'): str(root / 'live' / domain / name) for name in FILES[:4]}}
    require(settings.has_section('renewalparams')
            and all(settings.get('lineage', key, fallback=None) == path for key, path in expected.items())
            and settings.get('renewalparams', 'account', fallback=None) == value['account_id']
            and settings.get('renewalparams', 'server', fallback=None) == SERVER
            and settings.get('renewalparams', 'authenticator', fallback=None) == ('webroot' if mode == 'yandex-cdn' else 'standalone')
            and (mode != 'yandex-cdn' or settings.get('renewalparams', 'webroot_path', fallback='').rstrip(',') == '/var/www/spn-yandex-acme')
            and 'http-01' in settings.get('renewalparams', 'pref_challs', fallback='').replace(',', ' ').split()
            and not any(value.strip() for section in settings.sections() for key, value in settings.items(section)
                        if key.endswith('_hook')), 'Certificate renewal configuration changed')
    return {'certificateImported': True, 'domain': domain,
            'renewalAuthenticator': 'webroot' if mode == 'yandex-cdn' else 'standalone'}


def import_certificate(bundle_path, deployment, domain, *, root=Path('/etc/letsencrypt'),
                       node=Path('/opt/spn-reality-node'), uid=0, validator=validate_certificate,
                       mode='standalone'):
    """Resume only this exact owned bundle; do not adopt a foreign lineage."""
    require(isinstance(deployment, str) and re.fullmatch('[0-9a-f]{32}', deployment),
            'Invalid certificate import deployment')
    require(mode in ('standalone', 'yandex-cdn'), 'Invalid certificate renewal mode')
    root, node, bundle_path = Path(root), Path(node), Path(bundle_path)
    require(root.is_absolute() and node.is_absolute(), 'Certificate import paths must be absolute')
    _directory(node, uid)
    require(_read(node / '.spn-deployment', uid).decode() == deployment + '\n',
            'Certificate import deployment ownership differs')
    value = validate_bundle(_json(_read(bundle_path, uid)), domain)
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    marker = {'format': 1, 'domain': domain, 'deployment': deployment,
              'bundleHash': hashlib.sha256(encoded).hexdigest()}
    if mode == 'yandex-cdn':
        marker['renewalMode'] = mode
    marker_bytes = (json.dumps(marker, sort_keys=True) + '\n').encode()
    intent = node / 'tls-import.intent.json'
    complete = node / 'tls-import.complete.json'
    if complete.exists() or complete.is_symlink():
        require(_read(complete, uid) == marker_bytes and _read(intent, uid) == marker_bytes,
                'Certificate import bundle changed')
        return _audit_completed(root, node, value, marker_bytes, uid, validator, mode)
    validator(value['files'], domain)
    if intent.exists() or intent.is_symlink():
        require(_read(intent, uid) == marker_bytes, 'Certificate import bundle changed')
    else:
        # tls-domain is claimed only after all existing lineage locations have
        # been ruled out. A preexisting claim without our intent is not ours.
        for path in (node / 'tls-domain', root / 'live' / domain,
                     root / 'archive' / domain, root / 'renewal' / (domain + '.conf')):
            require(not path.exists() and not path.is_symlink(),
                    'An unrelated TLS certificate already exists')
        _write(intent, marker_bytes, uid)
    _write(node / 'tls-domain', (domain + '\n').encode(), uid)
    # Shared Certbot base directories may be distribution-created and 0755.
    if not root.exists():
        _directory(root.parent, uid, private=False)
        root.mkdir(mode=0o700)
    _directory(root, uid, private=False)
    for name in ('live', 'archive', 'renewal', 'accounts'):
        path = root / name
        if not path.exists():
            path.mkdir(mode=0o700)
        _directory(path, uid, private=False)
    for name in ('archive', 'live'):
        directory = root / name / domain
        _directory(directory, uid, create=True)
        claim = directory / '.spn-tls-import'
        if not claim.exists() and not claim.is_symlink():
            # Atomic file publication may leave only a private temporary file
            # after power loss. Do not delete it, or adopt certificate files.
            for leftover in directory.iterdir():
                require(leftover.name.startswith('.spn-tls-import-'),
                        'Unclaimed TLS certificate directory is not empty')
                _read(leftover, uid)
        _write(claim, marker_bytes, uid)
    for name in FILES[:4]:
        _write(root / 'archive' / domain / (name.removesuffix('.pem') + '1.pem'),
               value['files'][name].encode(), uid)
        link = root / 'live' / domain / name
        target = '../../archive/' + domain + '/' + name.removesuffix('.pem') + '1.pem'
        if link.exists() or link.is_symlink():
            require(link.is_symlink() and os.readlink(link) == target,
                    'Certificate lineage link changed')
        else:
            link.symlink_to(target)
    account = root / 'accounts'
    for part in (*ACCOUNT_SERVER.split('/'), value['account_id']):
        account /= part
        _directory(account, uid, create=True)
    for name in FILES[4:]:
        _write(account / name, value['files'][name].encode(), uid)
    _write(root / 'renewal' / (domain + '.conf'), _renewal(root, domain, value['account_id'], mode), uid)
    _write(node / 'tls-import.complete.json', marker_bytes, uid)
    return {'certificateImported': True, 'domain': domain,
            'renewalAuthenticator': 'webroot' if mode == 'yandex-cdn' else 'standalone'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--mode', choices=('standalone', 'yandex-cdn'), default='standalone')
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, 'Root is required for certificate import')
        result = import_certificate(args.bundle, args.deployment, args.domain, mode=args.mode,
                                    node=Path('/opt/spn-yandex-cdn' if args.mode == 'yandex-cdn' else '/opt/spn-reality-node'))
    except (ImportError, OSError, UnicodeError):
        print('Stopped: TLS certificate import failed; ownership and certificate must be checked',
              file=__import__('sys').stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
