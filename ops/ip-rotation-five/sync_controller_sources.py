#!/usr/bin/env python3
"""Update explicitly named source files only; no credentials or service changes."""
import argparse
import base64
import json
from pathlib import Path
import shlex
import subprocess
import sys

import capture_guests as access


REMOTE = r'''
import base64,hashlib,json,os,pathlib,stat,sys,tempfile
root=pathlib.Path('/opt/wayspn')
assert (root/'.controller-deployment').read_text().strip()=='276abbcb4f334b58ab25d9b2adc9d55c'
p=json.loads(sys.stdin.buffer.read(3000000));assert isinstance(p,dict) and 1<=len(p)<=64
for name,item in p.items():
 path=pathlib.PurePosixPath(name)
 assert len(path.parts) in (2,3) and path.suffix=='.py' and '..' not in path.parts
 assert path.parent==pathlib.PurePosixPath('spn_ip_failover') or path.parent==pathlib.PurePosixPath('ops/ip-rotation-five')
 raw=base64.b64decode(item['base64'],validate=True);assert 0<len(raw)<300000
 assert hashlib.sha256(raw).hexdigest()==item['sha256'];compile(raw,name,'exec')
 target=root/name
 if not target.parent.exists():target.parent.mkdir(mode=0o755)
 for parent in target.parents:
  m=parent.lstat();assert stat.S_ISDIR(m.st_mode) and m.st_uid==0 and not m.st_mode&0o022
 if target.exists() or target.is_symlink():
  m=target.lstat();assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and m.st_nlink==1 and not m.st_mode&0o022
 fd,tmp=tempfile.mkstemp(prefix='.spn-source-',dir=target.parent)
 try:
  with os.fdopen(fd,'wb')as f:os.fchmod(f.fileno(),0o644);f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,target)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
print('Named source files updated; services and credentials unchanged.')
'''


def main():
    import hashlib
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("files", nargs="+")
    args = p.parse_args()
    payload = {}
    for name in args.files:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py" or path.parent not in (
                Path("spn_ip_failover"), Path("ops/ip-rotation-five")):
            raise ValueError("Only explicit package/helper source paths are allowed")
        source = access.ROOT / path
        if source.is_symlink():
            raise ValueError("Source cannot be a symlink")
        raw = source.read_bytes()
        payload[name] = {"base64": base64.b64encode(raw).decode(),
                         "sha256": hashlib.sha256(raw).hexdigest()}
    proxy = shlex.join([sys.executable, str(Path(__file__).with_name("physical_connect.py")),
                        "--ip", "185.9.24.53", "--interface", "en0"])
    command = ["/usr/bin/ssh", *access.controller_socket_args(), "-i",
               str(access.ROOT / ".local-state/ip-rotation-20261115/controller_setup_ed25519"),
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=15", "-o", "ProxyCommand=" + proxy,
               "root@185.9.24.53", shlex.join(["python3", "-c", REMOTE])]
    result = subprocess.run(command, input=json.dumps(payload).encode(), check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
