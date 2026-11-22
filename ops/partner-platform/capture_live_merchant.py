"""One explicit read-only capture of the owner's live SPN merchant, no logs.

Never copies the main .env, mutates that VPS, contacts Telegram or creates a
payment. Only the two authorized merchant fields cross the SSH channel into a
new local 0600 file. VPS password is read hidden and never stored.
"""
import base64
import getpass
import json
import os
from pathlib import Path
import re
import pty
import select
import shlex
import subprocess
import sys
import time

OUTPUT = Path('/private/tmp/spn-owner-pilot-merchant-from-live-20261022.json')
REMOTE = r'''import base64,json,pathlib,re
values={}
for line in pathlib.Path('/root/spn-vpn-bot/.env').read_text().splitlines():
    name,sep,value=line.partition('=')
    if sep and name.strip() in ('YOOKASSA_SHOP_ID','YOOKASSA_SECRET_KEY'):
        value=value.strip()
        if len(value)>1 and value[0]==value[-1] and value[0] in ('"',"'"):
            value=value[1:-1]
        values[name.strip()]=value
shop,key=values.get('YOOKASSA_SHOP_ID',''),values.get('YOOKASSA_SECRET_KEY','')
assert re.fullmatch(r'[0-9]{1,20}',shop) and 10<=len(key)<=512 and not any(c.isspace() for c in key)
print('__MERCHANT_PRIVATE__'+base64.b64encode(json.dumps({'shop_id':shop,'secret_key':key}).encode()).decode()+'__END__')
'''


def main():
    if sys.argv[1:] != ['--capture'] or OUTPUT.exists() or OUTPUT.is_symlink():
        raise ValueError()
    password = getpass.getpass('SSH password (live SPN): ')
    master, slave = pty.openpty()
    child = subprocess.Popen(['/usr/bin/ssh',
        '-T', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
        '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
        '-o', 'NumberOfPasswordPrompts=1', '-o', 'ConnectTimeout=30',
        '-o', 'StrictHostKeyChecking=accept-new',
        '-o', 'UserKnownHostsFile=/private/tmp/wayspn-live-spn-known-hosts-20261022',
        'root@77.221.156.108', 'python3 -c ' + shlex.quote(REMOTE),
    ], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    try:
        buffer, sent, match = b'', False, None
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            if select.select([master], [], [], 1)[0]:
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 131072:
                    raise ValueError()
                if not sent and re.search(rb'(?i)password:', buffer):
                    os.write(master, (password + '\n').encode())
                    password, sent = '', True
                match = re.search(rb'__MERCHANT_PRIVATE__([A-Za-z0-9+/=]{1,4096})__END__', buffer)
            if child.poll() is not None:
                break
        if not match or child.wait(timeout=5) != 0:
            raise ValueError()
        data = json.loads(base64.b64decode(match.group(1), validate=True))
        if (set(data) != {'shop_id', 'secret_key'}
                or not isinstance(data['shop_id'], str) or not re.fullmatch(r'[0-9]{1,20}', data['shop_id'])
                or not isinstance(data['secret_key'], str) or not 10 <= len(data['secret_key']) <= 512
                or any(c.isspace() for c in data['secret_key'])):
            raise ValueError()
        fd = os.open(OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        print('{"captured":true,"fields":2,"source_vps_changed":false}')
    finally:
        password = ''
        os.close(master)
        if child.poll() is None:
            child.kill()
            child.wait()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"captured":false,"error":"private_capture_failed"}')
        sys.exit(1)
