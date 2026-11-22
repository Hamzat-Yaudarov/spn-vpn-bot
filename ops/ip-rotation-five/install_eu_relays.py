#!/usr/bin/env python3
"""Two non-root EU control relays; default is a local, network-free plan.

--apply uses controller-local private keys; only their public keys leave RF.
It changes SSH only for spn-ip-relay and validates a reload, never a restart.
The *:443 fence is not network isolation. The caller must also reject private
and reserved addresses, as spn_ip_failover.relay_worker does.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SERVICES = frozenset({"21e508f0-2ef2-47da-aee7-15dc8c66eb15", "4d6f358a-e75f-4dd8-a635-c6ad320f2732"})
CONTROLLER = "185.9.24.53"
POLICY = b"""# Way SPN: restricted outside-check relay only.
Match User spn-ip-relay
    AllowTcpForwarding local
    AllowStreamLocalForwarding no
    PermitTunnel no
    GatewayPorts no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    ForceCommand /usr/bin/false
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PubkeyAuthentication yes
    AuthenticationMethods publickey
    AuthorizedKeysFile /var/lib/spn-ip-relay/.ssh/authorized_keys
    PermitOpen *:443
    PermitListen none
Match all
"""


def load_key_setup():
    spec = importlib.util.spec_from_file_location("_spn_relay_key_utils", Path(__file__).with_name("install_guest_keys.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KEY_SETUP = load_key_setup()
# Reuse only the public-key parser and root-controlled atomic-file helpers.
REMOTE = KEY_SETUP.REMOTE.split("def readiness(service):", 1)[0] + "\nPOLICY=" + repr(POLICY) + "\n" + r'''
import pwd
SERVICES={"21e508f0-2ef2-47da-aee7-15dc8c66eb15","4d6f358a-e75f-4dd8-a635-c6ad320f2732"}
USER='spn-ip-relay'
HOME='/var/lib/spn-ip-relay'
DROPIN='/etc/ssh/sshd_config.d/91-spn-ip-relay.conf'
OPTIONS=b'restrict,port-forwarding,from="185.9.24.53,127.0.0.1",permitopen="*:443",command="/usr/bin/false"'
EXPECTED={'allowtcpforwarding':'local','allowstreamlocalforwarding':'no','permittunnel':'no',
 'gatewayports':'no','permittty':'no','x11forwarding':'no','allowagentforwarding':'no',
 'forcecommand':'/usr/bin/false','passwordauthentication':'no','kbdinteractiveauthentication':'no',
 'pubkeyauthentication':'yes','authenticationmethods':'publickey',
 'authorizedkeysfile':'/var/lib/spn-ip-relay/.ssh/authorized_keys','permitopen':'*:443','permitlisten':'none'}

class Commands:
 def run(self,argv):
  result=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
   env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/nonexistent','LC_ALL':'C'},timeout=30,check=False)
  need(result.returncode==0 and len(result.stdout)<=MAXIMUM,'A fixed account/sshd command failed')
  return result.stdout
 def syntax(self):self.run(['/usr/sbin/sshd','-t'])
 def effective(self,user,ip,source='185.9.24.53'):
  need(source in ('185.9.24.53','127.0.0.1'),'Unexpected relay SSH source')
  return self.run(['/usr/sbin/sshd','-T','-C','user='+user+',host=localhost,addr='+source+',laddr='+ip+',lport=22'])
 def service_ready(self):
  need(self.run(['/usr/bin/systemctl','is-active','ssh']).strip()==b'active','SSH service is not active')
  need(self.run(['/usr/bin/systemctl','show','ssh','-p','CanReload','--value']).strip()==b'yes',
       'SSH does not expose a safe reload; restart is forbidden')
 def reload(self):self.run(['/usr/bin/systemctl','reload','ssh'])

class Accounts:
 def lookup(self):
  try:p=pwd.getpwnam(USER)
  except KeyError:return None
  return {'name':p.pw_name,'uid':p.pw_uid,'gid':p.pw_gid,'home':p.pw_dir,'shell':p.pw_shell}
 def create(self,commands):
  commands.run(['/usr/sbin/useradd','--system','--user-group','--no-create-home',
                '--home-dir',HOME,'--shell','/bin/sh',USER])
 def locked(self,commands):
  fields=commands.run(['/usr/bin/passwd','-S',USER]).split()
  need(len(fields)>=2 and fields[0]==USER.encode() and fields[1] in (b'L',b'LK'),
       'Relay account password is not locked')

class UserFiles(Files):
 def __init__(self,root,uid,gid,admin_uid=0):
  super().__init__(root,uid);self.gid,self.admin_uid=gid,admin_uid;self.home=self.path(HOME)
 def parents(self,path):
  need(path==self.root or self.root in path.parents,'Path escapes installation root')
  for parent in path.parents:
   if parent!=self.root and self.root not in parent.parents:continue
   uid=self.uid if parent==self.home or self.home in parent.parents else self.admin_uid
   info=parent.lstat()
   need(stat.S_ISDIR(info.st_mode) and info.st_uid==uid and not info.st_mode&0o022,'Unsafe relay home ancestors')
 def directory(self,path):
  self.parents(path)
  try:
   path.mkdir(mode=0o700)
   info=path.lstat()
   if (info.st_uid,info.st_gid)!=(self.uid,self.gid):os.chown(path,self.uid,self.gid)
  except FileExistsError:pass
  self.require_directory(path)
 def replace(self,path,before,metadata,after):
  fd,name=tempfile.mkstemp(prefix='.spn-relay-key-',dir=path.parent)
  try:
   with os.fdopen(fd,'wb')as stream:
    os.fchmod(stream.fileno(),0o600)
    info=os.fstat(stream.fileno())
    if (info.st_uid,info.st_gid)!=(self.uid,self.gid):os.fchown(stream.fileno(),self.uid,self.gid)
    stream.write(after);stream.flush();os.fsync(stream.fileno())
   latest,meta=self.read(path,absent=True)
   need(latest==before and meta==metadata,'Relay authorized_keys changed concurrently')
   os.replace(name,path);self.sync(path.parent)
  finally:
   if os.path.exists(name):os.unlink(name)

def policy_file(fs,path):
 fs.parents(path)
 try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 except FileNotFoundError:return None,None
 with os.fdopen(fd,'rb')as stream:
  info=os.fstat(stream.fileno())
  need(stat.S_ISREG(info.st_mode) and info.st_uid==fs.uid and info.st_nlink==1
       and stat.S_IMODE(info.st_mode) in (0o600,0o644),'Unsafe existing SSH drop-in')
  raw=stream.read(MAXIMUM+1);need(len(raw)<=MAXIMUM,'SSH drop-in exceeds limit')
 return raw,(info.st_dev,info.st_ino,info.st_ctime_ns,info.st_uid,info.st_gid,info.st_mode)

def restrictions(raw):
 values={}
 for line in raw.decode().splitlines():
  if not line:continue
  key,separator,value=line.partition(' ')
  need(separator,'Unknown effective SSH output')
  if key not in EXPECTED and key!='disableforwarding':continue
  need(key not in values,'Ambiguous effective relay SSH setting')
  values[key]=value
 need(all(values.get(key)==value for key,value in EXPECTED.items()),'New relay SSH restrictions were not confirmed')
 need(values.get('disableforwarding','no')=='no','Global policy prohibits this SSH relay')

def ensure_account(fs,directory,accounts,commands):
 path=directory/'account.json'
 saved,_=fs.read(path,absent=True)
 current=accounts.lookup()
 if current is None:
  need(saved is None,'A previously owned relay account was removed; manual review is required')
  fs.once(directory/'account-intent.json',encoded({'format':1,'name':USER,'home':HOME,'shell':'/bin/sh'}))
  accounts.create(commands)
  current=accounts.lookup()
 else:
  need(saved is not None,'Existing spn-ip-relay user is not owned by this installer')
 need(isinstance(current,dict) and set(current)=={'name','uid','gid','home','shell'}
      and current['name']==USER and current['home']==HOME and current['shell']=='/bin/sh'
      and type(current['uid'])is int and current['uid']>0 and type(current['gid'])is int and current['gid']>0,
      'Relay account identity is not the dedicated non-root account')
 if saved is not None:need(json.loads(saved)==current,'Owned relay account identity changed')
 else:fs.once(path,encoded(current))
 accounts.locked(commands)
 return current

def install(request,files=None,accounts=None,commands=None):
 need(isinstance(request,dict) and set(request)=={'service_id','public_key','ip','apply'}
      and request['service_id']in SERVICES and request['apply']is True,'Invalid two-service relay setup request')
 import ipaddress
 ip=ipaddress.IPv4Address(request['ip'])
 need(ip.is_global and str(ip)==request['ip'],'Expected the current public guest IPv4')
 key=public_key(request['public_key']);service=request['service_id']
 fs=files or Files();accounts=accounts or Accounts();commands=commands or Commands()
 prefix=fs.path('/opt/spn-ip-failover-guest');fs.require_directory(prefix)
 marker,_=fs.read(prefix/'.service-id');need(marker==(service+'\n').encode(),'Guest staging belongs to another service')
 directory=fs.path('/var/lib/spn-ip-relay-setup');fs.directory(directory)
 fs.once(directory/'.service-id',(service+'\n').encode())
 lock=os.open(directory/'lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
 try:
  info=os.fstat(lock);need(stat.S_ISREG(info.st_mode) and info.st_uid==fs.uid and info.st_nlink==1
                         and stat.S_IMODE(info.st_mode)==0o600,'Unsafe relay setup lock')
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  commands.syntax();commands.service_ready()
  # A reverse-management connection reaches guest sshd through loopback,
  # including its local address. Check both Match Address/LocalAddress paths.
  contexts=((request['ip'],'185.9.24.53'),('127.0.0.1','127.0.0.1'))
  baseline={context:commands.effective('root',*context) for context in contexts}
  identity=ensure_account(fs,directory,accounts,commands)
  userfiles=UserFiles(fs.root,identity['uid'],identity['gid'],admin_uid=fs.uid)
  home=userfiles.path(HOME);userfiles.directory(home)
  ssh=home/'.ssh';userfiles.directory(ssh)
  auth=ssh/'authorized_keys'
  before_auth,auth_meta=userfiles.read(auth,absent=True)
  after_auth,key_changed=render_authorized(before_auth or b'',request['public_key'],service)
  dropin=fs.path(DROPIN);before_policy,policy_meta=policy_file(fs,dropin)
  need(before_policy is None or before_policy==POLICY,'Existing relay SSH drop-in is foreign; refusing overwrite')
  complete,_=fs.read(directory/'complete.json',absent=True)
  need(complete is None or not key_changed and before_policy==POLICY,
       'Previously owned relay access/policy was removed; manual review is required')
  intent=encoded({'format':1,'service_id':service,'public_key_sha256':digest(key),'policy_sha256':digest(POLICY)})
  fs.once(directory/'intent.json',intent)
  backup_path=directory/'ssh-dropin.before.json'
  backup,_=fs.read(backup_path,absent=True)
  if backup is None:
   fs.once(backup_path,encoded({'was_absent':before_policy is None,
          'base64':base64.b64encode(before_policy or b'').decode(),'sha256':digest(before_policy or b'')}))
  created_policy=before_policy is None
  if created_policy:fs.once(dropin,POLICY)
  try:
   commands.syntax()
   for context in contexts:
    need(commands.effective('root',*context)==baseline[context],'Root SSH policy changed; reload forbidden')
    restrictions(commands.effective(USER,*context))
  except Exception:
   # Roll back only this invocation's new file, before any reload. A prior
   # identical file is never removed, especially if a relay key already exists.
   if created_policy:
    latest,_meta=policy_file(fs,dropin)
    need(latest==POLICY,'New drop-in changed before rollback')
    dropin.unlink();fs.sync(dropin.parent)
    commands.syntax()
    for context in contexts:
     need(commands.effective('root',*context)==baseline[context],'Root policy rollback was not confirmed')
   raise
  loaded,_=fs.read(directory/'policy-loaded.json',absent=True)
  if loaded is None:
   commands.reload()
   for context in contexts:
    need(commands.effective('root',*context)==baseline[context],'Root policy changed across SSH reload')
    restrictions(commands.effective(USER,*context))
   fs.once(directory/'policy-loaded.json',intent)
  else:need(loaded==intent,'Loaded relay policy receipt identifies another key/configuration')
  # Forwarding restrictions are loaded before the key can authenticate.
  if key_changed:
   fs.once(directory/'authorized_keys.before.json',encoded({'was_absent':before_auth is None,
           'base64':base64.b64encode(before_auth or b'').decode(),'sha256':digest(before_auth or b'')}))
   userfiles.replace(auth,before_auth,auth_meta,after_auth)
  current,_=userfiles.read(auth)
  need(current==after_auth and render_authorized(current,request['public_key'],service)==(current,False),
       'Relay key readback was not confirmed')
  fs.once(directory/'complete.json',intent)
  return {'ok':True,'service_id':service,'user':USER,'already_installed':complete is not None,
          'ssh_reload_only':True,'root_policy_unchanged':True,'vpn_restarted':False,'network_restarted':False,
          'forwarding':'public-destinations-in-caller; TCP443-only-in-sshd'}
 finally:os.close(lock)

if __name__=='__main__':
 try:
  need(os.geteuid()==0,'Relay setup requires root')
  raw=sys.stdin.buffer.read(16385);need(0<len(raw)<=16384,'Invalid relay setup input')
  print(json.dumps(install(json.loads(raw)),separators=(',',':')))
 except Exception:
  print('Restricted EU relay setup not confirmed; details suppressed.',file=sys.stderr)
  raise SystemExit(1)
'''

CONTROLLER_KEYS = r'''
import json,os,pathlib,pwd,stat,subprocess,sys
allowed={'21e508f0-2ef2-47da-aee7-15dc8c66eb15','4d6f358a-e75f-4dd8-a635-c6ad320f2732'}
services=json.loads(sys.stdin.buffer.read(4096))
assert isinstance(services,list)and services and len(set(services))==len(services)and set(services)<=allowed
assert pathlib.Path('/opt/wayspn/.controller-deployment').read_text().strip()=='276abbcb4f334b58ab25d9b2adc9d55c'
account=pwd.getpwnam('spn-failover');directory=pathlib.Path('/var/lib/spn-ip-failover/.ssh')
for path in (directory.parent,directory):
 m=path.lstat();assert stat.S_ISDIR(m.st_mode)and m.st_uid==account.pw_uid and stat.S_IMODE(m.st_mode)==0o700
result={}
for service in services:
 key=directory/('relay_'+service+'_ed25519');pub=directory/(key.name+'.pub')
 if not key.exists()and not pub.exists():
  command=['/usr/sbin/runuser','-u','spn-failover','--','/usr/bin/ssh-keygen','-q','-t','ed25519','-N','','-C','spn-ip-relay:'+service,'-f',str(key)]
  completed=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30,check=False)
  assert completed.returncode==0
 for path,private in ((key,True),(pub,False)):
  m=path.lstat();assert stat.S_ISREG(m.st_mode)and m.st_uid==account.pw_uid and m.st_nlink==1
  assert (stat.S_IMODE(m.st_mode)==0o600) if private else (not m.st_mode&0o022)
 # The private key is used only by an RF-local process. Its bytes are never
 # read into this program, serialized, printed or sent off the RF VPS.
 completed=subprocess.run(['/usr/sbin/runuser','-u','spn-failover','--','/usr/bin/ssh-keygen','-y','-f',str(key)],
  stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20,check=False)
 assert completed.returncode==0 and 0<len(completed.stdout)<2048
 fd=os.open(pub,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb')as stream:raw=stream.read(2049)
 assert 0<len(raw)<=2048 and raw.split()[:2]==completed.stdout.split()[:2]
 result[service]=b' '.join(raw.split()[:2]).decode('ascii')
print(json.dumps(result,separators=(',',':')))
'''


def worker_namespace():
    namespace = {"__name__": "_spn_relay_setup_worker"}
    exec(compile(REMOTE, "<spn-eu-relay-setup>", "exec"), namespace)
    return namespace


def controller_keys(services):
    proxy = shlex.join([sys.executable, str(Path(__file__).with_name("physical_connect.py")),
                        "--ip", CONTROLLER, "--interface", "en0"])
    command = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-i",
               str(ROOT / ".local-state/ip-rotation-20261115/controller_setup_ed25519"),
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=15", "-o", "ProxyCommand=" + proxy, "root@" + CONTROLLER,
               shlex.join(["/usr/bin/python3", "-c", CONTROLLER_KEYS])]
    result = subprocess.run(command, input=json.dumps(sorted(services)).encode(), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=120, check=False)
    if result.returncode or not 0 < len(result.stdout) < 16384:
        raise RuntimeError("RF relay public-key preparation was not confirmed")
    keys = json.loads(result.stdout)
    if not isinstance(keys, dict) or set(keys) != services:
        raise RuntimeError("RF relay public-key service scope differs")
    parse = worker_namespace()["public_key"]
    keys = {service: parse(key).decode() for service, key in keys.items()}
    if len(set(keys.values())) != len(keys):
        raise RuntimeError("RF relay keys are not independent")
    return keys


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
        print(json.dumps({"plan_only": True, "service_count": len(selected), "network_used": False,
                          "credentials_read": False, "remote_writes": False,
                          "new_user": "spn-ip-relay", "ssh_change": "validated MatchUser drop-in and reload only",
                          "private_keys_remain_on": CONTROLLER, "vpn_or_network_restart": False}))
        return
    if args.billing_config is None or args.output is None:
        raise ValueError("Apply requires private billing settings and output directory")
    output = args.output.resolve(strict=True)
    if output.stat().st_uid != os.getuid() or output.stat().st_mode & 0o777 != 0o700:
        raise ValueError("Output must be an owned private directory")
    keys = controller_keys(selected)
    access = KEY_SETUP.load_access()
    rows = [row for row in access.scope.SELECTED if row["service_id"] in selected]
    if {row["service_id"] for row in rows} != selected:
        raise RuntimeError("Relay selection differs from the two approved services")
    settings = access.Settings.load(args.billing_config)
    with access.browser(settings) as page:
        page.route("**/*", lambda route: route.continue_() if route.request.method in ("GET", "HEAD", "OPTIONS") else route.abort())
        for row in rows:
            ui = access.OneCentPage(page, row["service_id"], row["service_name"])
            ui.open()
            inventory = ui.inventory()
            if len(inventory) != 1 or not inventory[0].primary or inventory[0].ip != row["ip_at_enrollment"]:
                raise RuntimeError("Guest IPv4 changed before relay setup")
            known, alias = access.trust(row, output)
            transport = access.reachable_transport(row["ip_at_enrollment"], args.ssh_interface)
            command = access.ssh_command(row, known, alias, transport)
            secret = access.credentials(page.context, row)
            env = {**os.environ, "SSH_ASKPASS": str(Path(__file__).with_name("guest_password_askpass.py")),
                   "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0", "SPN_GUEST_SETUP_PASSWORD": secret}
            payload = {"service_id": row["service_id"], "public_key": keys[row["service_id"]],
                       "ip": row["ip_at_enrollment"], "apply": True}
            try:
                result = subprocess.run(command + [shlex.join(["/usr/bin/python3", "-c", REMOTE])],
                                        input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, start_new_session=True,
                                        timeout=240, check=False)
            finally:
                env.pop("SPN_GUEST_SETUP_PASSWORD", None)
                del secret
            if result.returncode or len(result.stdout) > 4096:
                raise RuntimeError("Restricted relay setup was not confirmed for " + row["key"])
            report = json.loads(result.stdout)
            if report.get("ok") is not True or report.get("service_id") != row["service_id"]:
                raise RuntimeError("Relay receipt identifies another service")
            receipt = {key: value for key, value in report.items() if key != "already_installed"}
            access.private_write(output / (row["key"] + ".eu-relay.json"), (json.dumps(receipt, sort_keys=True) + "\n").encode())
            print(json.dumps({"server": row["key"], **report}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("EU relay setup not confirmed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
