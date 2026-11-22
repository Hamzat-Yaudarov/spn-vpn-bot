#!/usr/bin/env python3
"""Install the source-only reserve fleet bundle on a systemd Debian/Ubuntu host.

Default is a local plan. Secrets and deployment journals must be provisioned
separately in private paths. No VPN node or public firewall is modified.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import stat
import subprocess
import sys
import tempfile


INSTALL = Path('/opt/spn-reserve-fleet')
HOME = Path('/var/lib/spn-reserve-fleet')
ACCOUNT = 'spn-reserve'
UNIT = Path('/etc/systemd/system/spn-reserve-fleet.service')
OWNER = 'spn-reserve-fleet-installer-v1'
STATE = HOME / '.spn-reserve-fleet'
ENV_FILE = HOME / 'config/remnawave.env'
REQUIREMENTS = ['fastapi>=0.110,<1', 'uvicorn>=0.25,<1']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_files(source):
    source = Path(source).expanduser().absolute()
    require(not any(path.is_symlink() for path in (source, *source.parents)), 'Source must not contain symlink parents')
    manifest_path = source / 'runtime-manifest.json'
    require(manifest_path.is_file() and not manifest_path.is_symlink(), 'Missing runtime manifest')
    encoded = manifest_path.read_bytes()
    require(len(encoded) < 65536, 'Runtime manifest is too large')
    manifest = json.loads(encoded)
    require(manifest.get('format') == 1 and isinstance(manifest.get('files'), dict)
            and 1 <= len(manifest['files']) <= 128, 'Invalid runtime manifest')
    files = {}
    for name, digest in manifest['files'].items():
        relative = PurePosixPath(name)
        require(isinstance(name, str) and not relative.is_absolute() and '..' not in relative.parts
                and name == relative.as_posix() and not name.startswith('.')
                and isinstance(digest, str) and re.fullmatch('[a-f0-9]{64}', digest), 'Unsafe manifest path/hash')
        allowed = ((name.startswith('spn_reserve_fleet/') and relative.suffix in ('.py', '.service', '.js', '.css', '.html'))
                   or (name.startswith('ops/universal-reality/') and relative.suffix in ('.py', '.sh', '.cfg'))
                   or (name.startswith('ops/universal-yandex/') and relative.suffix in ('.py', '.sh', '.cfg'))
                   or name == 'ops/universal-cascade/host_checks.py'
                   or name in ('ops/universal-reality/scanner_lists.json', 'ops/universal-reality/tls-site-index.html')
                   or name in ('ops/universal-direct/deploy.py', 'ops/host-health-audit/e2e.py'))
        require(allowed and not relative.name.startswith('test_'), 'Manifest contains non-runtime files')
        path = source / name
        require(not any(part.is_symlink() for part in (path, *path.parents)) and path.is_file(), 'Unsafe source file')
        raw = path.read_bytes()
        require(len(raw) <= 2_000_000 and hashlib.sha256(raw).hexdigest() == digest, 'Source digest differs')
        files[name] = raw
    required = {'spn_reserve_fleet/cli.py', 'spn_reserve_fleet/web.py', 'spn_reserve_fleet/runtime.py',
                'ops/universal-reality/change_ip.py', 'ops/universal-reality/replace.py',
                'ops/universal-reality/deploy.py', 'ops/universal-direct/deploy.py', 'ops/host-health-audit/e2e.py'}
    require(required <= files.keys(), 'Bundle is missing runtime dependencies')
    return files, hashlib.sha256(encoded).hexdigest()


def run(command):
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)


def root_directory(path, mode=0o755, *, owner_uid=0):
    require(not any(part.is_symlink() for part in (path, *path.parents)), 'Unsafe installation directory')
    path.mkdir(parents=True, mode=mode, exist_ok=True)
    info = path.stat()
    require(info.st_uid == owner_uid and stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022,
            'Installation directory must be root-owned without group/world writes')
    # mkdir's requested mode is reduced by the invoking SSH session's umask.
    # These root-owned source directories must be traversable by the service.
    path.chmod(mode)


@contextmanager
def public_code_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def repair_public_code_tree(root, *, owner_uid=0):
    """Repair an owned source/venv tree created under a restrictive old umask.

    Only call for releases and venv, never INSTALL as a whole or service HOME.
    Validate the entire tree first; symlinks (e.g. venv Python) are not followed
    or chmod'ed, and hardlinks/special files are refused.
    """
    require(root.is_dir() and not root.is_symlink(), 'Unsafe public code root')
    paths = [root]
    for current, directories, files in os.walk(root, followlinks=False):
        paths.extend(Path(current) / name for name in directories + files)
    checked = []
    for path in paths:
        info = path.lstat()
        require(info.st_uid == owner_uid, 'Public code must be owned by installer root')
        if stat.S_ISLNK(info.st_mode):
            continue
        require(stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1),
                'Unsafe public code file type or permissions')
        mode = 0o755 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111 else 0o644
        checked.append((path, mode))
    for path, mode in checked:
        path.chmod(mode)


def marker():
    path = INSTALL / '.installer-owner'
    require(not any(part.is_symlink() for part in (INSTALL, *INSTALL.parents)), 'Unsafe installation directory')
    if INSTALL.exists():
        require(path.is_file() and not path.is_symlink() and path.read_text() == OWNER + '\n',
                'Existing installation belongs to another installer')
    elif path.is_symlink():
        raise ValueError('Unsafe installation marker')


def service_text():
    return f'''# {OWNER}
[Unit]
Description=WAY SPN SSH monitor and reserve IP panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={ACCOUNT}
Group={ACCOUNT}
WorkingDirectory={INSTALL}/current
Environment=HOME={HOME}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={INSTALL}/venv/bin/python -m spn_reserve_fleet --data-dir {STATE} --env-file {ENV_FILE} serve --host 127.0.0.1 --port 8787
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def validate_unit():
    if UNIT.exists() or UNIT.is_symlink():
        require(UNIT.is_file() and not UNIT.is_symlink() and UNIT.read_text().startswith('# ' + OWNER + '\n'),
                'Existing systemd unit is not owned by this installer')


def private_owned(path, uid, gid, mode=0o700):
    require(not any(part.is_symlink() for part in (path, *path.parents)), 'Unsafe service state directory')
    if not path.exists():
        path.mkdir(mode=mode, parents=True)
        os.chown(path, uid, gid)
    info = path.stat()
    require(info.st_uid == uid and info.st_gid == gid and stat.S_IMODE(info.st_mode) == mode,
            'Service private directory owner/mode differs')


def validate_private_file(path, uid, *, nonempty=True):
    require(path.is_file() and not path.is_symlink(), 'Missing private prerequisite: ' + path.name)
    info = path.stat()
    require(info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
            and (not nonempty or info.st_size > 0), 'Private prerequisite owner/mode differs: ' + path.name)


def install(args):
    files, source_hash = source_files(args.source)
    plan = {'planOnly': not args.apply, 'install': str(INSTALL), 'home': str(HOME), 'user': ACCOUNT,
            'state': str(STATE), 'envFile': str(ENV_FILE), 'sourceHash': source_hash, 'files': len(files),
            'panelBind': '127.0.0.1:8787', 'startsService': bool(args.apply and args.start),
            'changesVpnNodes': False, 'changesFirewall': False, 'includesSecrets': False}
    if not args.apply:
        return plan
    # Apply uses a predictable umask for source directories and pip/venv
    # subprocesses. Runtime/private directories still use explicit 0700/0600.
    with public_code_umask():
        return apply_install(args, files, source_hash, plan)


def apply_install(args, files, source_hash, plan):
    require(sys.platform.startswith('linux') and os.geteuid() == 0, 'Apply requires Linux root')
    release = Path('/etc/os-release').read_text()
    require(re.search(r'^ID=(?:"?)(?:ubuntu|debian)(?:"?)$', release, re.M) is not None,
            'Only Debian/Ubuntu systemd hosts are supported')
    require(Path('/run/systemd/system').is_dir(), 'A running systemd host is required')
    marker()
    validate_unit()
    try:
        account = pwd.getpwnam(ACCOUNT)
    except KeyError:
        account = None
    require(account is None or (account.pw_dir == str(HOME) and account.pw_uid != 0
                               and account.pw_shell in ('/usr/sbin/nologin', '/sbin/nologin')),
            'Existing service user has unexpected home/shell')
    require(not HOME.exists() or account is not None, 'An unrelated service home already exists')
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=600', 'update'])
    run(['apt-get', '-o', 'DPkg::Lock::Timeout=600', 'install', '-y', 'python3', 'python3-venv',
         'openssh-client', 'dnsutils', 'ca-certificates', 'curl', 'certbot', 'openssl'])
    if account is None:
        run(['useradd', '--system', '--user-group', '--home-dir', str(HOME), '--shell', '/usr/sbin/nologin', ACCOUNT])
        account = pwd.getpwnam(ACCOUNT)
    root_directory(INSTALL)
    ownership = INSTALL / '.installer-owner'
    if not ownership.exists():
        ownership.write_text(OWNER + '\n')
        ownership.chmod(0o600)
    for path in (HOME, STATE, HOME / 'config', HOME / '.ssh', HOME / '.spn-reality-deploy'):
        private_owned(path, account.pw_uid, account.pw_gid)
    releases = INSTALL / 'releases'
    root_directory(releases)
    destination = releases / source_hash
    if destination.exists():
        for name, raw in files.items():
            path = destination / name
            require(path.is_file() and not path.is_symlink() and path.read_bytes() == raw,
                    'Installed release content differs')
    else:
        stage = Path(tempfile.mkdtemp(prefix='.release-', dir=releases))
        stage.chmod(0o755)
        for name, raw in files.items():
            path = stage / name
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o644)
        (stage / 'runtime-manifest.json').write_bytes((Path(args.source) / 'runtime-manifest.json').read_bytes())
        (stage / 'runtime-manifest.json').chmod(0o644)
        stage.rename(destination)
    venv = INSTALL / 'venv'
    require(not venv.is_symlink(), 'Unsafe virtual environment')
    if not (venv / 'bin/python').exists():
        run(['python3', '-m', 'venv', str(venv)])
    run([str(venv / 'bin/python'), '-m', 'pip', 'install', '--disable-pip-version-check', *REQUIREMENTS])
    # This also repairs installations made by the former version with an
    # inherited root SSH umask077, without widening private state permissions.
    repair_public_code_tree(releases)
    repair_public_code_tree(venv)
    current = INSTALL / 'current'
    if current.exists() or current.is_symlink():
        require(current.is_symlink() and current.resolve().parent == releases,
                'Existing current path is not an owned release symlink')
    replacement = INSTALL / '.current-next'
    require(not replacement.exists() and not replacement.is_symlink(), 'Previous release activation is incomplete')
    replacement.symlink_to(destination)
    replacement.replace(current)
    UNIT.write_text(service_text())
    UNIT.chmod(0o644)
    run(['systemctl', 'daemon-reload'])
    if args.start:
        validate_private_file(STATE / 'password.json', account.pw_uid)
        # Empty inventories may be started as a read-only panel. Provisioned
        # servers still require API/SSH prerequisites before enabling auto.
        run(['systemctl', 'enable', '--now', 'spn-reserve-fleet.service'])
        run(['systemctl', 'restart', 'spn-reserve-fleet.service'])
        run(['systemctl', 'is-active', '--quiet', 'spn-reserve-fleet.service'])
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--start', action='store_true', help='Start the service after password provisioning')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args), ensure_ascii=False))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print('Install stopped: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
