#!/usr/bin/env python3
"""Install only five scoped controller public keys after guest readiness checks.

Default is a local plan without reading credentials or contacting a server.
The --apply path reads public .pub files, never controller private keys.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = "185.9.24.53"
DEPLOYMENT = "276abbcb4f334b58ab25d9b2adc9d55c"
SERVICES = frozenset({
    "f5c0287a-636e-4981-95c7-80b24c9559c0", "21e508f0-2ef2-47da-aee7-15dc8c66eb15",
    "9b0316d5-fa83-4bf8-b6dc-8202b66622c9", "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d",
    "4d6f358a-e75f-4dd8-a635-c6ad320f2732",
})


REMOTE = r'''
import base64,fcntl,hashlib,json,os,pathlib,re,stat,struct,subprocess,sys,tempfile

SERVICES = {
 "f5c0287a-636e-4981-95c7-80b24c9559c0", "21e508f0-2ef2-47da-aee7-15dc8c66eb15",
 "9b0316d5-fa83-4bf8-b6dc-8202b66622c9", "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d",
 "4d6f358a-e75f-4dd8-a635-c6ad320f2732",
}
OPTIONS = b'restrict,from="185.9.24.53",command="/usr/bin/python3 -I /opt/spn-ip-failover-guest/guest_rpc.py"'
MAXIMUM = 1024 * 1024
PHASE = 'validate_request'

def need(condition, message):
 if not condition:raise ValueError(message)

def digest(raw):return hashlib.sha256(raw).hexdigest()

def public_key(value):
 need(isinstance(value,str) and 0<len(value)<=2048 and not any(c in value for c in ('\r','\n','\x00')), 'Invalid public key')
 fields=value.split()
 need(len(fields) in (2,3) and fields[0]=='ssh-ed25519', 'Only a plain Ed25519 public key is accepted')
 blob=base64.b64decode(fields[1],validate=True)
 need(len(blob)==51 and blob[:4]==struct.pack('>I',11) and blob[4:15]==b'ssh-ed25519'
      and blob[15:19]==struct.pack('>I',32), 'Invalid Ed25519 key encoding')
 need(base64.b64encode(blob).decode()==fields[1], 'Public key is not canonical')
 return ('ssh-ed25519 '+fields[1]).encode()

def render_authorized(before, value, service):
 need(service in SERVICES and isinstance(before,bytes) and len(before)<=MAXIMUM and b'\x00' not in before,
      'Invalid pinned authorized_keys input')
 key=public_key(value)
 blob=key.split()[1]
 expected=OPTIONS+b' '+key
 found=0
 # Scan conservatively: even a matching key embedded in a comment is a reason
 # to stop, not an excuse to append a second possibly unrestricted entry.
 pattern=re.compile(rb'(?:^|[ \t])ssh-ed25519[ \t]+([A-Za-z0-9+/=]+)(?=[ \t]|$)')
 for line in before.splitlines():
  for match in pattern.finditer(line):
   if match.group(1)!=blob:continue
   need(line==expected or line.startswith(expected+b' ') or line.startswith(expected+b'\t'),
        'The same public key already has foreign or different options')
   found+=1
 if found:return before, False
 suffix=expected+b' spn-ip-failover:'+service.encode()+b'\n'
 return before+(b'' if not before or before.endswith(b'\n') else b'\n')+suffix, True

def encoded(value):return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()

class Files:
 def __init__(self,root=pathlib.Path('/'),uid=0):
  self.root,self.uid=pathlib.Path(root),uid
 def path(self,name):
  need(name.startswith('/') and '..' not in pathlib.Path(name).parts,'Invalid fixed path')
  return self.root/name.lstrip('/')
 def parents(self,path):
  need(path==self.root or self.root in path.parents,'Path escapes installation root')
  for parent in path.parents:
   if parent!=self.root and self.root not in parent.parents:continue
   info=parent.lstat()
   need(stat.S_ISDIR(info.st_mode) and info.st_uid==self.uid and not info.st_mode&0o022,
        'Unsafe root-controlled directory')
 def directory(self,path):
  self.parents(path)
  try:path.mkdir(mode=0o700)
  except FileExistsError:pass
  self.require_directory(path)
 def require_directory(self,path):
  self.parents(path)
  info=path.lstat()
  need(stat.S_ISDIR(info.st_mode) and info.st_uid==self.uid and stat.S_IMODE(info.st_mode)==0o700,
       'Expected a root-owned private directory')
 def read(self,path,mode=0o600,absent=False):
  self.parents(path)
  try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
  except FileNotFoundError:
   need(absent,'Expected owned file is missing')
   return None,None
  with os.fdopen(fd,'rb') as stream:
   info=os.fstat(stream.fileno())
   need(stat.S_ISREG(info.st_mode) and info.st_uid==self.uid and info.st_nlink==1
        and stat.S_IMODE(info.st_mode)==mode,'Unsafe root file')
   raw=stream.read(MAXIMUM+1)
   need(len(raw)<=MAXIMUM,'Owned file exceeds limit')
  return raw,(info.st_dev,info.st_ino,info.st_ctime_ns,info.st_uid,info.st_gid,info.st_mode)
 def once(self,path,raw):
  old,_=self.read(path,absent=True)
  if old is not None:
   need(old==raw,'Existing immutable key-enrollment artifact differs')
   return
  fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as stream:
   stream.write(raw);stream.flush();os.fsync(stream.fileno())
  self.sync(path.parent)
 def sync(self,path):
  fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  try:os.fsync(fd)
  finally:os.close(fd)
 def replace(self,path,before,metadata,after):
  fd,name=tempfile.mkstemp(prefix='.spn-key-',dir=path.parent)
  try:
   with os.fdopen(fd,'wb') as stream:
    os.fchmod(stream.fileno(),0o600);stream.write(after);stream.flush();os.fsync(stream.fileno())
   latest,meta=self.read(path,absent=True)
   need(latest==before and meta==metadata,'authorized_keys changed concurrently')
   os.replace(name,path);self.sync(path.parent)
  finally:
   if os.path.exists(name):os.unlink(name)

def readiness(service):
 request=encoded({'operation':'check','server_id':service})
 result=subprocess.run(['/usr/bin/python3','-I','/opt/spn-ip-failover-guest/guest_rpc.py'],
  input=request,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90,check=False,
  env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/nonexistent','LC_ALL':'C'})
 need(result.returncode==0 and len(result.stdout)<=1024 and json.loads(result.stdout)=={'ok':True},
      'Guest readiness did not confirm the enrolled network')

def install(request,files=None,check=readiness):
 global PHASE
 need(isinstance(request,dict) and set(request)=={'service_id','public_key','rpc_sha256','apply'}
      and request['service_id'] in SERVICES and request['apply'] is True,'Invalid pinned key-install request')
 service=request['service_id']
 key=public_key(request['public_key'])
 need(isinstance(request['rpc_sha256'],str) and re.fullmatch('[0-9a-f]{64}',request['rpc_sha256']),
      'Invalid trusted RPC digest')
 fs=files or Files()
 PHASE='verify_staged_helper'
 prefix=fs.path('/opt/spn-ip-failover-guest')
 fs.require_directory(prefix)
 marker,_=fs.read(prefix/'.service-id')
 need(marker==(service+'\n').encode(),'Helper staging belongs to another service')
 rpc,_=fs.read(prefix/'guest_rpc.py',mode=0o644)
 need(digest(rpc)==request['rpc_sha256'],'The staged RPC is not the reviewed helper')
 config,_=fs.read(fs.path('/etc/spn-ip-failover-guest.json'))
 parsed=json.loads(config)
 need(isinstance(parsed,dict) and parsed.get('server_id')==service and parsed.get('method')=='netplan',
      'The guest has not been enrolled for this service')
 PHASE='guest_readiness'
 check(service)
 # Readiness and ownership precede any SSH access changes.
 PHASE='prepare_private_ssh_directory'
 home=fs.path('/root');fs.parents(home/'.ssh')
 ssh=home/'.ssh';fs.directory(ssh)
 state=fs.path('/var/lib/spn-ip-failover-key-enrollment');fs.directory(state)
 directory=state/service;fs.directory(directory)
 lock=os.open(directory/'lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
 try:
  info=os.fstat(lock)
  need(stat.S_ISREG(info.st_mode) and info.st_uid==fs.uid and stat.S_IMODE(info.st_mode)==0o600
       and info.st_nlink==1,'Unsafe key-install lock')
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  PHASE='read_authorized_keys'
  path=ssh/'authorized_keys'
  before,metadata=fs.read(path,absent=True)
  after,changed=render_authorized(before or b'',request['public_key'],service)
  if not changed:
   intent_raw,_=fs.read(directory/'intent.json',absent=True)
   complete_raw,_=fs.read(directory/'complete.json',absent=True)
   if intent_raw is not None and complete_raw is None:
    intent=json.loads(intent_raw)
    need(intent.get('service_id')==service and intent.get('public_key_sha256')==digest(key)
         and intent.get('after_sha256')==digest(before),'Pending key installation changed; manual review is required')
    fs.once(directory/'complete.json',intent_raw)
   return {'ok':True,'service_id':service,'already_installed':True,'network_changed':False}
  completed,_=fs.read(directory/'complete.json',absent=True)
  need(completed is None,'A previously installed managed key was removed; manual review is required')
  backup=encoded({'format':1,'service_id':service,'was_absent':before is None,
                 'base64':base64.b64encode(before or b'').decode(),'sha256':digest(before or b'')})
  fs.once(directory/'authorized_keys.before.json',backup)
  intent=encoded({'format':1,'service_id':service,'public_key_sha256':digest(key),
                  'before_sha256':digest(before or b''),'after_sha256':digest(after)})
  fs.once(directory/'intent.json',intent)
  fs.replace(path,before,metadata,after)
  current,_=fs.read(path)
  need(current==after and render_authorized(current,request['public_key'],service)==(current,False),
       'Restricted authorized_keys readback failed')
  fs.once(directory/'complete.json',intent)
  return {'ok':True,'service_id':service,'already_installed':False,'network_changed':False}
 finally:os.close(lock)

if __name__=='__main__':
 try:
  need(os.geteuid()==0,'Key installer requires root')
  raw=sys.stdin.buffer.read(16385);need(0<len(raw)<=16384,'Invalid key-install input size')
  print(json.dumps(install(json.loads(raw)),separators=(',',':')))
 except Exception as exc:
  print(json.dumps({'ok':False,'phase':PHASE,'error_type':type(exc).__name__}))
  print('Restricted guest key installation not confirmed; details suppressed.',file=sys.stderr)
  raise SystemExit(1)
'''


PUBLIC_READ = r'''
import json,os,pathlib,pwd,stat,sys
services={"f5c0287a-636e-4981-95c7-80b24c9559c0","21e508f0-2ef2-47da-aee7-15dc8c66eb15",
"9b0316d5-fa83-4bf8-b6dc-8202b66622c9","626633f5-3bf3-48c7-aaba-9bbf43f8ea9d","4d6f358a-e75f-4dd8-a635-c6ad320f2732"}
assert pathlib.Path('/opt/wayspn/.controller-deployment').read_text().strip()=='276abbcb4f334b58ab25d9b2adc9d55c'
owner=pwd.getpwnam('spn-failover').pw_uid
directory=pathlib.Path('/var/lib/spn-ip-failover/.ssh')
for path in [directory.parent,directory]:
 m=path.lstat();assert stat.S_ISDIR(m.st_mode) and m.st_uid==owner and stat.S_IMODE(m.st_mode)==0o700
result={}
for service in sorted(services):
 path=directory/('guest_'+service+'_ed25519.pub')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb')as stream:
  m=os.fstat(stream.fileno());assert stat.S_ISREG(m.st_mode) and m.st_uid==owner and m.st_nlink==1 and not m.st_mode&0o022
  raw=stream.read(2049);assert 0<len(raw)<=2048
 result[service]=raw.decode('ascii').strip()
print(json.dumps(result,separators=(',',':')))
'''


def worker_namespace():
    namespace = {"__name__": "_spn_guest_key_authorizer"}
    exec(compile(REMOTE, "<restricted-guest-key-authorizer>", "exec"), namespace)
    return namespace


def read_controller_public_keys():
    access = load_access()
    proxy = shlex.join([sys.executable, str(Path(__file__).with_name("physical_connect.py")),
                        "--ip", CONTROLLER, "--interface", "en0"])
    command = ["/usr/bin/ssh", "-F", "/dev/null", "-T", *access.controller_socket_args(), "-i",
               str(ROOT / ".local-state/ip-rotation-20261115/controller_setup_ed25519"),
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=15", "-o", "ProxyCommand=" + proxy, "root@" + CONTROLLER,
               shlex.join(["/usr/bin/python3", "-c", PUBLIC_READ])]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=100, check=False)
    if result.returncode or not 0 < len(result.stdout) < 16384:
        raise RuntimeError("Controller public-key inventory was not confirmed")
    values = json.loads(result.stdout)
    if not isinstance(values, dict) or set(values) != SERVICES:
        raise RuntimeError("Controller public-key scope differs")
    authorizer = worker_namespace()
    normalized = {service: authorizer["public_key"](value).decode() for service, value in values.items()}
    if len(set(normalized.values())) != len(SERVICES):
        raise RuntimeError("Controller guest public keys are not independent")
    return normalized


def load_access():
    spec = importlib.util.spec_from_file_location("_spn_guest_key_access", Path(__file__).with_name("capture_guests.py"))
    access = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(access)
    return access


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=sorted(SERVICES))
    parser.add_argument("--billing-config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ssh-interface", default="auto")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    selected = {args.service} if args.service else SERVICES
    if not args.apply:
        print(json.dumps({"plan_only": True, "service_count": len(selected), "controller": CONTROLLER,
                          "network_used": False, "credentials_read": False, "remote_writes": False,
                          "forced_command": "/usr/bin/python3 -I /opt/spn-ip-failover-guest/guest_rpc.py"}))
        return
    if args.billing_config is None or args.output is None:
        raise ValueError("Apply requires private billing settings and output directory")
    output = args.output.resolve(strict=True)
    if output.stat().st_uid != os.getuid() or output.stat().st_mode & 0o777 != 0o700:
        raise ValueError("Output must be a private owned directory")
    keys = read_controller_public_keys()
    access = load_access()
    rows = [row for row in access.scope.SELECTED if row["service_id"] in selected]
    if {row["service_id"] for row in rows} != selected:
        raise RuntimeError("Current guest selection differs from the fixed service allowlist")
    rpc_spec = importlib.util.spec_from_file_location("_spn_guest_stage", Path(__file__).with_name("stage_guest.py"))
    stage = importlib.util.module_from_spec(rpc_spec)
    # stage_guest imports capture_guests by filename; the trusted directory is
    # inserted only for this local staging import, never on an untrusted guest.
    sys.path.insert(0, str(Path(__file__).parent))
    rpc_spec.loader.exec_module(stage)
    settings = access.Settings.load(args.billing_config)
    with access.browser(settings) as page:
        page.route("**/*", lambda route: route.continue_() if route.request.method in ("GET", "HEAD", "OPTIONS") else route.abort())
        for row in rows:
            ui = access.OneCentPage(page, row["service_id"], row["service_name"])
            ui.open()
            inventory = ui.inventory()
            if len(inventory) != 1 or not inventory[0].primary or inventory[0].ip != row["ip_at_enrollment"]:
                raise RuntimeError("Guest IP changed before restricted-key installation")
            known, alias = access.trust(row, output)
            transport = access.reachable_transport(row["ip_at_enrollment"], args.ssh_interface)
            command = access.ssh_command(row, known, alias, transport)
            secret = access.credentials(page.context, row)
            env = {**os.environ, "SSH_ASKPASS": str(Path(__file__).with_name("guest_password_askpass.py")),
                   "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0", "SPN_GUEST_SETUP_PASSWORD": secret}
            request = {"service_id": row["service_id"], "public_key": keys[row["service_id"]],
                       "rpc_sha256": hashlib.sha256(stage.RPC).hexdigest(), "apply": True}
            try:
                result = subprocess.run(command + [shlex.join(["/usr/bin/python3", "-c", REMOTE])],
                                        input=json.dumps(request).encode(), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, start_new_session=True,
                                        timeout=180, check=False)
            finally:
                env.pop("SPN_GUEST_SETUP_PASSWORD", None)
                del secret
            if result.returncode or len(result.stdout) > 4096:
                phase = "ssh_or_remote_helper"
                try:
                    failed = json.loads(result.stdout)
                    candidate = failed.get("phase")
                    if isinstance(candidate, str) and candidate.replace("_", "").isalpha():
                        phase = candidate
                except (ValueError, TypeError):
                    pass
                raise RuntimeError("Restricted key installation was not confirmed for " + row["key"] + ": " + phase)
            report = json.loads(result.stdout)
            if report.get("ok") is not True or report.get("service_id") != row["service_id"]:
                raise RuntimeError("Restricted key receipt identifies another service")
            receipt = {key: report[key] for key in ("ok", "service_id", "network_changed")}
            access.private_write(output / (row["key"] + ".key-installed.json"), (json.dumps(receipt, sort_keys=True) + "\n").encode())
            print(json.dumps({"server": row["key"], **report}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Passwords, environment and arbitrary browser/SSH errors stay private.
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print("Guest key setup not confirmed: " + message, file=sys.stderr)
        raise SystemExit(1)
