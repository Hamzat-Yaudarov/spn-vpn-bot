#!/usr/bin/env python3
"""Send only the two approved saved billing credentials via encrypted SSH stdin."""
from __future__ import annotations

import base64
import argparse
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '.local-state/ip-failover-ru-20261113'
STATE = ROOT / '.local-state/ip-rotation-20261115'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',
                        help='Requires explicit approval to export billing credentials to 185.9.24.53')
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({'plan_only': True, 'destination': '185.9.24.53',
                          'credential_names': ['ONECENT_EMAIL', 'ONECENT_PASSWORD'],
                          'credentials_read': False, 'network_used': False,
                          'requires_explicit_destination_approval': True}))
        return
    source = BASE / 'provider-credentials.private.env'
    assert not source.is_symlink() and source.stat().st_uid == os.getuid()
    assert source.stat().st_mode & 0o077 == 0
    lines = [line for line in source.read_text().splitlines()
             if re.match(r'^\s*(?:export\s+)?(?:ONECENT_EMAIL|ONECENT_PASSWORD)\s*=', line)]
    values = dotenv_values(stream=io.StringIO('\n'.join(lines)), interpolate=False)
    assert set(values) == {'ONECENT_EMAIL', 'ONECENT_PASSWORD'}
    assert all(isinstance(v, str) and 0 < len(v) <= 4096 and not any(c in v for c in ('\x00', '\r', '\n'))
               for v in values.values())
    script = Path(__file__).with_name('audit_onecent_readonly.py').read_bytes()
    payload = json.dumps({'onecent_email': values['ONECENT_EMAIL'],
                          'onecent_password': values['ONECENT_PASSWORD'],
                          'audit_script': base64.b64encode(script).decode()}).encode()
    remote = r'''
import base64,json,os,pathlib,stat,sys
p=json.loads(sys.stdin.buffer.read(100000))
assert set(p)=={'onecent_email','onecent_password','audit_script'}
assert pathlib.Path('/opt/wayspn/.controller-deployment').read_text().strip()=='276abbcb4f334b58ab25d9b2adc9d55c'
directory=pathlib.Path('/etc/spn-ip-failover/credentials')
i=directory.lstat(); assert stat.S_ISDIR(i.st_mode) and i.st_uid==0 and stat.S_IMODE(i.st_mode)==0o700
for name in ('onecent_email','onecent_password'):
    value=p[name]; assert isinstance(value,str) and 0<len(value)<=4096 and not any(c in value for c in ('\x00','\r','\n'))
    raw=(value+'\n').encode(); path=directory/name
    if path.exists():
        m=path.lstat(); assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and stat.S_IMODE(m.st_mode)==0o600 and path.read_bytes()==raw
    else:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
raw=base64.b64decode(p['audit_script'],validate=True)
assert 0<len(raw)<50000
target=pathlib.Path('/opt/wayspn/ops/ip-rotation-five')
if not target.exists(): target.mkdir(mode=0o755)
m=target.lstat();assert stat.S_ISDIR(m.st_mode) and m.st_uid==0 and not m.st_mode&0o022
path=target/'audit_onecent_readonly.py'
if path.exists():
    m=path.lstat();assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and not m.st_mode&0o022
    assert path.read_bytes()==raw
else:
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o644)
    os.fchmod(fd,0o644)
    with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
print('Only approved 1CENT credentials stored root:root 0600; read-only audit staged.')
'''
    proxy = shlex.join([sys.executable, str(Path(__file__).with_name('physical_connect.py')),
                        '--ip', '185.9.24.53', '--interface', 'en0'])
    command = ['/usr/bin/ssh', '-i', str(STATE / 'controller_setup_ed25519'), '-o', 'IdentitiesOnly=yes',
               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
               '-o', 'ProxyCommand=' + proxy, 'root@185.9.24.53', shlex.join(['python3', '-c', remote])]
    result = subprocess.run(command, input=payload, check=False)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Credential staging not confirmed: ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
