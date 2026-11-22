#!/usr/bin/env python3
"""Operator transport for approved helper setup only; never orders/releases IPs."""
from __future__ import annotations
import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import sys

import capture_guests as access
from spn_ip_failover.onecent_fields import parse_api_addresses

ROOT = access.ROOT
STATE = ROOT / ".local-state/ip-rotation-20261115"
PREFIX = "/opt/spn-ip-failover-guest"
SOURCE_NAMES = {"reconcile_sweden_epoch.py", "install_reverse_management.py", "reverse_management_model.py",
                "install_eu_relays.py", "install_guest_keys.py", "enroll_netplan.py", "guest_enrollment.py"}


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


STAGE = r'''
import base64,hashlib,json,os,pathlib,stat,sys,tempfile
p=json.loads(sys.stdin.buffer.read(1000000));assert set(p)=={'service_id','sources'}
root=pathlib.Path('/opt/spn-ip-failover-guest')
m=root.lstat();assert stat.S_ISDIR(m.st_mode) and m.st_uid==0 and stat.S_IMODE(m.st_mode)==0o700
assert (root/'.service-id').read_text().strip()==p['service_id']
allowed={'reconcile_sweden_epoch.py','install_reverse_management.py','reverse_management_model.py',
         'install_eu_relays.py','install_guest_keys.py','enroll_netplan.py','guest_enrollment.py'}
assert set(p['sources'])<=allowed and p['sources']
for name,item in p['sources'].items():
 raw=base64.b64decode(item['base64'],validate=True);assert 0<len(raw)<300000 and hashlib.sha256(raw).hexdigest()==item['sha256']
 compile(raw,name,'exec');path=root/name
 if path.exists()or path.is_symlink():
  m=path.lstat();assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and m.st_nlink==1 and not m.st_mode&0o022
 fd,tmp=tempfile.mkstemp(prefix='.setup-source-',dir=root)
 try:
  with os.fdopen(fd,'wb')as f:os.fchmod(f.fileno(),0o644);f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
print('Scoped helper sources staged')
'''


SAVE = r'''
import base64,hashlib,json,os,pathlib,stat,sys,tempfile
p=json.loads(sys.stdin.buffer.read(2000000));assert set(p)=={'name','base64','sha256'}
import re
assert p['name']=='manual-snapshot.json' or re.fullmatch('manual-agreement-[0-9a-f]{64}\\.json',p['name'])
raw=base64.b64decode(p['base64'],validate=True);assert 0<len(raw)<1000000 and hashlib.sha256(raw).hexdigest()==p['sha256']
root=pathlib.Path('/opt/spn-ip-failover-guest');m=root.lstat()
assert stat.S_ISDIR(m.st_mode) and m.st_uid==0 and stat.S_IMODE(m.st_mode)==0o700
path=root/p['name']
if path.exists()or path.is_symlink():
 m=path.lstat();assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and m.st_nlink==1 and stat.S_IMODE(m.st_mode)==0o600
 assert path.read_bytes()==raw,'Existing frozen setup input differs'
else:
 fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb')as f:f.write(raw);f.flush();os.fsync(f.fileno())
print(json.dumps({'saved':p['name'],'sha256':p['sha256']}))
'''


class Guest:
    def __init__(self, page, row, output=STATE, *, expected_addresses=None, reverse_port=None):
        self.page, self.row, self.output = page, row, Path(output)
        self.temp = None
        self.process = None
        self.expected_addresses, self.reverse_port = expected_addresses, reverse_port

    def __enter__(self):
        # Setup reads only: exact API service identity and two stable allocation
        # reads avoid depending on React hydration. No billing endpoint writes.
        origin = "https://bill.1cent.host/api/services/" + self.row["service_id"]
        def get(url):
            response = self.page.context.request.get(url, max_redirects=0, timeout=15000)
            try:
                assert response.status == 200
                raw = response.body()
                assert 0 < len(raw) < 300000
                return json.loads(raw)
            finally:
                response.dispose()
        identity = get(origin)
        assert identity.get("id") == self.row["service_id"] and identity.get("name") == self.row["service_name"]
        assert identity.get("pendingOperation") is None and identity.get("migrating") is False
        first = parse_api_addresses(get(origin + "/ipv4/addresses"))
        assigned = parse_api_addresses(get(origin + "/ipv4/addresses"))
        assert first == assigned
        if self.expected_addresses is None:
            if len(assigned) != 1 or not assigned[0].primary or assigned[0].ip != self.row["ip_at_enrollment"]:
                raise RuntimeError("Current hosting assignment drifted")
        else:
            from reverse_management_model import SERVICE_PORTS
            assert self.reverse_port == SERVICE_PORTS[self.row["service_id"]]
            assert self.row["service_id"] in {'9b0316d5-fa83-4bf8-b6dc-8202b66622c9',
                '626633f5-3bf3-48c7-aaba-9bbf43f8ea9d','4d6f358a-e75f-4dd8-a635-c6ad320f2732'}
            assert {r.ip: r.primary for r in assigned} == self.expected_addresses and len(assigned) == len(self.expected_addresses)
        self.observed_addresses = assigned
        known, alias = access.trust(self.row, self.output)
        if self.reverse_port is None:
            transport = access.reachable_transport(self.row["ip_at_enrollment"], "auto")
            base = access.ssh_command(self.row, known, alias, transport)
        else:
            relay = rf_command()[:-1] + ['-W', '127.0.0.1:' + str(self.reverse_port), rf_command()[-1]]
            base = access.ssh_command(self.row, known, alias, None)
            base = base[:-1] + ['-o', 'ProxyCommand=' + shlex.join(relay), base[-1]]
        self.temp = tempfile.TemporaryDirectory(prefix="spn-guest-mgmt-", dir="/private/tmp")
        os.chmod(self.temp.name, 0o700)
        self.socket = str(Path(self.temp.name) / "ssh")
        secret = access.credentials(self.page.context, self.row)
        env = {**os.environ, "SSH_ASKPASS": str(Path(__file__).with_name("guest_password_askpass.py")),
               "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0", "SPN_GUEST_SETUP_PASSWORD": secret}
        try:
            self.process = subprocess.Popen(base[:-1] + ["-M", "-N", "-S", self.socket, "-o", "ControlPersist=no", base[-1]],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            env=env, start_new_session=True)
        finally:
            env.pop("SPN_GUEST_SETUP_PASSWORD", None)
            del secret
        self.command = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-S", self.socket,
                        "-o", "ControlMaster=no", "-o", "ProxyCommand=/usr/bin/false", base[-1]]
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            if Path(self.socket).exists():
                checked = subprocess.run(self.command[:-1] + ["-O", "check", self.command[-1]],
                                         stdin=subprocess.DEVNULL, capture_output=True)
                if checked.returncode == 0:
                    return self
            time.sleep(0.25)
        self.__exit__(None, None, None)
        raise RuntimeError("Trusted guest SSH master was not established")

    def __exit__(self, *_):
        if hasattr(self, "command"):
            subprocess.run(self.command[:-1] + ["-O", "exit", self.command[-1]],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.temp:
            self.temp.cleanup()

    def run(self, command, data=b"", timeout=180):
        result = subprocess.run(self.command + [command], input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
        if result.returncode or len(result.stdout) > 1500000:
            messages = [line for line in result.stderr.decode(errors="replace").splitlines()
                        if line.startswith(("Manual Sweden reconciliation stopped: ", "Reverse management setup stopped: "))
                        and len(line) < 400]
            raise RuntimeError(messages[0] if messages else "Scoped guest operation was not confirmed")
        return result.stdout

    def stage(self, names):
        assert set(names) <= SOURCE_NAMES
        sources = {}
        for name in names:
            raw = Path(__file__).with_name(name).read_bytes()
            sources[name] = {"base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}
        return self.run(shlex.join(["python3", "-c", STAGE]),
                        encoded({"service_id": self.row["service_id"], "sources": sources}))

    def save(self, name, raw):
        return self.run(shlex.join(["python3", "-c", SAVE]), encoded({
            "name": name, "base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}))

    def role(self, role, payload):
        return json.loads(self.run(shlex.join(["python3", "-I", PREFIX + "/install_reverse_management.py",
                                              "--role", role, "--service", self.row["service_id"], "--apply"]),
                                   encoded(payload)))


def browser():
    config = ROOT / ".local-state/ip-failover-ru-20261113/onecent-check.private.json"
    return access.browser(access.Settings.load(config))


def rf_command():
    proxy = shlex.join([sys.executable, str(Path(__file__).with_name("physical_connect.py")),
                        "--ip", "185.9.24.53", "--interface", "en0"])
    return ["/usr/bin/ssh", *access.controller_socket_args(), "-i", str(STATE / "controller_setup_ed25519"),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=20", "-o", "ProxyCommand=" + proxy, "root@185.9.24.53"]


def rf_run(command, data=b"", timeout=180):
    result = subprocess.run(rf_command() + [command], input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode or len(result.stdout) > 1500000:
        raise RuntimeError("Scoped RF setup operation was not confirmed")
    return result.stdout


RF_STAGE = STAGE.replace("assert set(p)=={'service_id','sources'}", "assert set(p)=={'sources'}") \
    .replace("root=pathlib.Path('/opt/spn-ip-failover-guest')", """root=pathlib.Path('/opt/wayspn/reverse-management-setup')
parent=root.parent;m=parent.lstat();assert stat.S_ISDIR(m.st_mode) and m.st_uid==0 and not m.st_mode&0o022
assert (parent/'.controller-deployment').read_text().strip()=='276abbcb4f334b58ab25d9b2adc9d55c'
try:root.mkdir(mode=0o700)
except FileExistsError:pass""") \
    .replace("assert (root/'.service-id').read_text().strip()==p['service_id']", "")


def rf_stage():
    names = {"install_reverse_management.py", "reverse_management_model.py", "install_eu_relays.py", "install_guest_keys.py"}
    sources = {}
    for name in names:
        raw = Path(__file__).with_name(name).read_bytes()
        sources[name] = {"base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}
    return rf_run(shlex.join(["python3", "-c", RF_STAGE]), encoded({"sources": sources}))


def rf_role(service, role, payload):
    return json.loads(rf_run(shlex.join(["python3", "-I", "/opt/wayspn/reverse-management-setup/install_reverse_management.py",
                                      "--role", role, "--service", service, "--apply"]), encoded(payload)))


RF_CHECK = r'''
import json,pathlib,subprocess,sys,time
sys.path.insert(0,'/opt/wayspn/reverse-management-setup')
import reverse_management_model as model
p=json.loads(sys.stdin.buffer.read(4096));service=p['service_id'];port=model.service_port(service)
assert set(p)=={'service_id','ip'}
cmd=['/usr/sbin/runuser','-u','spn-failover','--']+[s.replace('{address}',p['ip']) for s in model.gateway_ssh(service)]
deadline=time.monotonic()+70
while True:
 r=subprocess.run(cmd,input=json.dumps({'operation':'check','server_id':service}).encode(),capture_output=True,timeout=30)
 if r.returncode==0 and len(r.stdout)<1024 and json.loads(r.stdout)=={'ok':True}:break
 if time.monotonic()>deadline:raise RuntimeError('Pinned loopback RPC was not confirmed')
 time.sleep(3)
listeners=subprocess.run(['ss','-H','-ltn'],capture_output=True,check=True,text=True).stdout.splitlines()
binds=[row.split()[3] for row in listeners if row.split()[3].endswith(':'+str(port))]
assert binds==['127.0.0.1:'+str(port)],'Listener is absent or not loopback-only'
print(json.dumps({'ok':True,'service_id':service,'port':port,'user':'spn-failover',
 'fixed_rpc_check':True,'trusted_host_key':True,'loopback_only':True,'guest_ssh':model.gateway_ssh(service)}))
'''


def reverse_install(guest, rpc_public):
    service = guest.row["service_id"]
    guest.stage({"install_reverse_management.py", "reverse_management_model.py", "install_eu_relays.py", "install_guest_keys.py"})
    prepared = guest.role("guest-prepare", {"service_id": service, "apply": True})
    registration = rf_role(service, "rf-register", {"service_id": service, "public_key": prepared["public_key"], "apply": True})
    configured = guest.role("guest-activate", {"service_id": service, "controller_rpc_public_key": rpc_public,
                                               "registration": registration, "apply": True})
    checked = json.loads(rf_run(shlex.join(["python3", "-c", RF_CHECK]),
                               encoded({"service_id": service, "ip": guest.row["ip_at_enrollment"]}), timeout=120))
    access.private_write(STATE / (guest.row["key"] + ".reverse-verified.json"), encoded(checked))
    return {"configured": configured, "verified": checked}


RELAY_KNOWN = r'''
import json,os,pathlib,pwd,stat,sys
p=json.loads(sys.stdin.buffer.read(16000));allowed={'21e508f0-2ef2-47da-aee7-15dc8c66eb15','4d6f358a-e75f-4dd8-a635-c6ad320f2732'}
assert set(p)==allowed
assert pathlib.Path('/opt/wayspn/.controller-deployment').read_text().strip()=='276abbcb4f334b58ab25d9b2adc9d55c'
u=pwd.getpwnam('spn-failover');d=pathlib.Path('/var/lib/spn-ip-failover/.ssh');m=d.lstat()
assert stat.S_ISDIR(m.st_mode)and m.st_uid==u.pw_uid and stat.S_IMODE(m.st_mode)==0o700
raw=''.join('spn-ip-rotation-relay-'+s+' '+k+'\n' for s,k in sorted(p.items())).encode();target=d/'relay_known_hosts'
try:fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
except FileNotFoundError:
 fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb')as f:os.fchown(f.fileno(),u.pw_uid,u.pw_gid);f.write(raw);f.flush();os.fsync(f.fileno())
else:
 with os.fdopen(fd,'rb')as f:
  m=os.fstat(f.fileno());assert stat.S_ISREG(m.st_mode)and m.st_uid==u.pw_uid and m.st_nlink==1 and stat.S_IMODE(m.st_mode)==0o600 and f.read(16001)==raw
print(json.dumps({'relay_known_hosts_pinned':2,'private_keys_transferred':False}))
'''


RELAY_VERIFY = r'''
import json,socket,subprocess,sys,time
sys.path.insert(0,'/opt/wayspn/reverse-management-setup')
import reverse_management_model as model
p=json.loads(sys.stdin.buffer.read(4096));s=p['service_id'];port=model.service_port(s)
assert s in {'21e508f0-2ef2-47da-aee7-15dc8c66eb15','4d6f358a-e75f-4dd8-a635-c6ad320f2732'}and set(p)=={'service_id','ip'}
sock=socket.socket();sock.bind(('127.0.0.1',0));local=sock.getsockname()[1];sock.close()
base=['/usr/sbin/runuser','-u','spn-failover','--','/usr/bin/ssh','-4','-F','/dev/null','-N',
 '-o','HostName=127.0.0.1','-p',str(port),'-l','spn-ip-relay','-o','HostKeyAlias=spn-ip-rotation-relay-'+s,
 '-i','/var/lib/spn-ip-failover/.ssh/relay_'+s+'_ed25519',
 '-o','UserKnownHostsFile=/var/lib/spn-ip-failover/.ssh/relay_known_hosts','-o','GlobalKnownHostsFile=/dev/null',
 '-o','StrictHostKeyChecking=yes','-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','ExitOnForwardFailure=yes',
 '-o','PasswordAuthentication=no','-o','KbdInteractiveAuthentication=no','-o','ConnectTimeout=10',
 '-L','127.0.0.1:'+str(local)+':www.gstatic.com:443',p['ip']]
child=subprocess.Popen(base,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
try:
 deadline=time.monotonic()+15
 while True:
  assert child.poll()is None,'Restricted relay SSH setup failed'
  try:
   sock=socket.create_connection(('127.0.0.1',local),timeout=1);sock.close();break
  except OSError:
   if time.monotonic()>deadline:raise
   time.sleep(.25)
 r=subprocess.run(['curl','--noproxy','*','--connect-to','www.gstatic.com:443:127.0.0.1:'+str(local),
  '--connect-timeout','5','--max-time','12','--silent','--show-error','--output','/dev/null','--write-out','%{http_code}',
  'https://www.gstatic.com/generate_204'],capture_output=True,timeout=17)
 assert r.returncode==0 and r.stdout==b'204','HTTPS control via restricted reverse relay failed'
 print(json.dumps({'ok':True,'service_id':s,'port':port,'user':'spn-ip-relay','https_control':204,'private_key_exported':False}))
finally:
 child.terminate()
 try:child.wait(timeout=5)
 except subprocess.TimeoutExpired:child.kill();child.wait()
'''


def relay_prepare():
    import install_eu_relays as eu
    public = json.loads(rf_run(shlex.join(["python3", "-c", eu.CONTROLLER_KEYS]), encoded(sorted(eu.SERVICES))))
    known = {}
    for row in access.scope.SELECTED:
        if row["service_id"] not in eu.SERVICES:
            continue
        path, _ = access.trust(row, STATE)
        fields = {" ".join(p[1:]) for line in path.read_text().splitlines()
                  if len(p := line.split()) == 3 and p[1] == "ssh-ed25519"}
        assert len(fields) == 1
        known[row["service_id"]] = fields.pop()
    rf_run(shlex.join(["python3", "-c", RELAY_KNOWN]), encoded(known))
    return public


def relay_install(guest, public):
    import install_eu_relays as eu
    service = guest.row["service_id"]
    guest.stage({"install_eu_relays.py", "install_guest_keys.py"})
    receipt = json.loads(guest.run(shlex.join(["python3", "-c", eu.REMOTE]), encoded({
        "service_id": service, "public_key": public, "ip": guest.row["ip_at_enrollment"], "apply": True})))
    assert receipt.get("ok") is True
    access.private_write(STATE / (guest.row["key"] + ".eu-relay.json"), encoded(receipt))
    checked = json.loads(rf_run(shlex.join(["python3", "-c", RELAY_VERIFY]),
                               encoded({"service_id": service, "ip": guest.row["ip_at_enrollment"]}), timeout=90))
    access.private_write(STATE / (guest.row["key"] + ".eu-relay-verified.json"), encoded(checked))
    return {"installed": receipt, "verified": checked}


if __name__ == "__main__":
    print(json.dumps({"transport_only": True, "network_used": False}))
