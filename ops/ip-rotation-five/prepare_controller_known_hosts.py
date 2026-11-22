#!/usr/bin/env python3
"""Copy previously trusted guest public host keys, never private keys, to RF."""
import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import capture_guests as access


def payload(output):
    lines = []
    for row in access.scope.SELECTED:
        known, _alias = access.trust(row, output)
        keys = {fields[2] for line in known.read_text().splitlines()
                if len(fields := line.split()) == 3 and fields[1] == "ssh-ed25519"}
        if len(keys) != 1:
            raise ValueError("Expected one previously accepted Ed25519 host key")
        key = next(iter(keys))
        base64.b64decode(key, validate=True)
        lines.append(row["service_id"] + " ssh-ed25519 " + key)
    return ("\n".join(lines) + "\n").encode()


REMOTE = r'''
import os,pwd,stat,sys
raw=sys.stdin.buffer.read(16001)
assert 0<len(raw)<=16000 and len(raw.splitlines())==5
assert open('/opt/wayspn/.controller-deployment').read().strip()=='276abbcb4f334b58ab25d9b2adc9d55c'
user=pwd.getpwnam('spn-failover'); directory='/var/lib/spn-ip-failover/.ssh'
m=os.lstat(directory); assert stat.S_ISDIR(m.st_mode) and m.st_uid==user.pw_uid and stat.S_IMODE(m.st_mode)==0o700
path=directory+'/guest_known_hosts'
try:
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
except FileNotFoundError:
 fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as stream:
  os.fchown(stream.fileno(),user.pw_uid,user.pw_gid);stream.write(raw);stream.flush();os.fsync(stream.fileno())
else:
 with os.fdopen(fd,'rb') as stream:
  m=os.fstat(stream.fileno()); assert stat.S_ISREG(m.st_mode) and m.st_uid==user.pw_uid and m.st_nlink==1 and stat.S_IMODE(m.st_mode)==0o600 and stream.read(16001)==raw
fd=os.open(directory,os.O_DIRECTORY|os.O_RDONLY|os.O_NOFOLLOW)
try:os.fsync(fd)
finally:os.close(fd)
print('Five previously trusted guest host keys pinned; no private keys transferred.')
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not args.apply:
        print(json.dumps({"plan_only": True, "destination": "185.9.24.53",
                          "private_keys_transferred": False, "network_used": False}))
        return
    output = access.ROOT / ".local-state/ip-rotation-20261115"
    assert output.stat().st_uid == os.getuid() and output.stat().st_mode & 0o777 == 0o700
    proxy = shlex.join([sys.executable, str(Path(__file__).with_name("physical_connect.py")),
                        "--ip", "185.9.24.53", "--interface", "en0"])
    command = ["/usr/bin/ssh", "-i", str(output / "controller_setup_ed25519"), "-o", "IdentitiesOnly=yes",
               "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
               "-o", "ProxyCommand=" + proxy, "root@185.9.24.53", shlex.join(["python3", "-c", REMOTE])]
    result = subprocess.run(command, input=payload(output), check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
