#!/usr/bin/env python3
"""Stage a root-owned helper and capture a private Netplan plan. No network writes."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import capture_guests as access


PREFIX = "/opt/spn-ip-failover-guest"
ROOT = access.ROOT
PHASE = "plan"
FILES = (
    "spn_ip_failover/__init__.py",
    "spn_ip_failover/managed_guest.py",
    "spn_ip_failover/netplan_guest.py",
)

REMOTE_INSTALL = r'''
import base64,hashlib,json,os,pathlib,stat,sys,tempfile
data=json.loads(sys.stdin.buffer.read(1000000))
assert set(data)=={'service_id','files'}
allowed={'f5c0287a-636e-4981-95c7-80b24c9559c0','21e508f0-2ef2-47da-aee7-15dc8c66eb15','9b0316d5-fa83-4bf8-b6dc-8202b66622c9','626633f5-3bf3-48c7-aaba-9bbf43f8ea9d','4d6f358a-e75f-4dd8-a635-c6ad320f2732'}
assert data['service_id'] in allowed and os.geteuid()==0
root=pathlib.Path('/opt/spn-ip-failover-guest')
for parent in root.parents:
 m=parent.lstat();assert stat.S_ISDIR(m.st_mode) and m.st_uid==0 and not m.st_mode&0o022
marker=root/'.service-id'
if root.exists():
 m=root.lstat();assert stat.S_ISDIR(m.st_mode) and m.st_uid==0 and stat.S_IMODE(m.st_mode)==0o700
 m=marker.lstat();assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and stat.S_IMODE(m.st_mode)==0o600
 assert marker.read_text().strip()==data['service_id']
else:
 root.mkdir(mode=0o700)
 fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'w')as f:f.write(data['service_id']+'\n');f.flush();os.fsync(f.fileno())
expected={'spn_ip_failover/__init__.py','spn_ip_failover/managed_guest.py','spn_ip_failover/netplan_guest.py','guest_enrollment.py','enroll_netplan.py','guest_rpc.py'}
assert set(data['files'])==expected
for name,item in data['files'].items():
 assert set(item)=={'base64','sha256'}
 raw=base64.b64decode(item['base64'],validate=True)
 assert 0<len(raw)<300000 and hashlib.sha256(raw).hexdigest()==item['sha256']
 compile(raw,name,'exec')
 target=root/name
 if not target.parent.exists():target.parent.mkdir(mode=0o700)
 m=target.parent.lstat();assert stat.S_ISDIR(m.st_mode) and m.st_uid==0 and not m.st_mode&0o022
 if target.exists()or target.is_symlink():
  m=target.lstat();assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and m.st_nlink==1 and not m.st_mode&0o022
 fd,tmp=tempfile.mkstemp(prefix='.stage-',dir=target.parent)
 try:
  with os.fdopen(fd,'wb')as f:os.fchmod(f.fileno(),0o644);f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(tmp,target)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
print(json.dumps({'staged':True,'service_id':data['service_id'],'network_changed':False}))
'''

RPC = b'''#!/usr/bin/python3
import sys
sys.path.insert(0,"/opt/spn-ip-failover-guest")
from spn_ip_failover.netplan_guest import main
raise SystemExit(main(["--config","/etc/spn-ip-failover-guest.json"]))
'''


def package(row):
    import hashlib
    files = {name: (ROOT / name).read_bytes() for name in FILES}
    for name in ("guest_enrollment.py", "enroll_netplan.py"):
        files[name] = Path(__file__).with_name(name).read_bytes()
    files["guest_rpc.py"] = RPC
    return json.dumps({"service_id": row["service_id"], "files": {
        name: {"base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in files.items()}}).encode()


def run_remote(command, remote, data, env):
    result = subprocess.run(command + [remote], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, start_new_session=True, timeout=180, check=False)
    if result.returncode:
        # Helper messages are static/metadata only. Never emit a browser or SSH
        # exception with arbitrary fields, environment or password arguments.
        details = [line for line in result.stderr.decode(errors="replace").splitlines()
                   if line.startswith(("Netplan enrollment stopped: ", "Guest enrollment stopped: "))
                   and len(line) < 350]
        raise RuntimeError(details[0] if details else "Scoped SSH/helper result not confirmed")
    if len(result.stdout) > 1000000:
        raise RuntimeError("Helper output exceeds private capture limit")
    return result.stdout


def main():
    global PHASE
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--service", required=True, choices=[row["service_id"] for row in access.scope.SELECTED])
    p.add_argument("--billing-config", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--ssh-interface", default="auto")
    p.add_argument("--stage", action="store_true", help="Install only code, capture and dry plan; never enroll")
    p.add_argument("--enroll", action="store_true", help="Apply the already staged and pinned handoff; no network/VPN restart")
    args = p.parse_args()
    row = next(row for row in access.scope.SELECTED if row["service_id"] == args.service)
    if args.stage and args.enroll:
        p.error("Choose stage or enroll, not both")
    if not args.stage and not args.enroll:
        print(json.dumps({"plan_only": True, "service": args.service, "ip": row["ip_at_enrollment"],
                          "network_changed": False, "credential_read": False}))
        return
    output = args.output.resolve(strict=True)
    assert output.stat().st_uid == os.getuid() and output.stat().st_mode & 0o777 == 0o700
    settings = access.Settings.load(args.billing_config)
    PHASE = "open_browser"
    with access.browser(settings) as page:
        page.route("**/*", lambda r: r.continue_() if r.request.method in ("GET", "HEAD", "OPTIONS") else r.abort())
        ui = access.OneCentPage(page, row["service_id"], row["service_name"])
        PHASE = "open_service"
        ui.open()
        PHASE = "read_inventory"
        rows = ui.inventory()
        assert len(rows) == 1 and rows[0].primary and rows[0].ip == row["ip_at_enrollment"]
        known, alias = access.trust(row, output)
        PHASE = "choose_ssh_transport"
        command = access.ssh_command(row, known, alias, access.reachable_transport(row["ip_at_enrollment"], args.ssh_interface))
        PHASE = "read_approved_root_password"
        secret = access.credentials(page.context, row)
        env = {**os.environ, "SSH_ASKPASS": str(Path(__file__).with_name("guest_password_askpass.py")),
               "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0", "SPN_GUEST_SETUP_PASSWORD": secret}
        try:
            if args.enroll:
                import hashlib
                pinned = (output / (row["key"] + ".enrollment.json")).read_bytes()
                envelope = json.loads(pinned)
                assert envelope["service_id"] == row["service_id"] and envelope["ip"] == row["ip_at_enrollment"]
                check = r'''
import base64,hashlib,json,pathlib,stat,sys
p=json.loads(sys.stdin.buffer.read(1000000));root=pathlib.Path('/opt/spn-ip-failover-guest')
assert (root/'.service-id').read_text().strip()==p['service_id']
for name,item in p['files'].items():
 path=root/name;m=path.lstat()
 assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and m.st_nlink==1 and not m.st_mode&0o022
 assert hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256']
path=root/'enrollment-snapshot.json';m=path.lstat()
assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and m.st_nlink==1 and stat.S_IMODE(m.st_mode)==0o600
assert path.read_bytes()==base64.b64decode(p['snapshot'],validate=True)
print('Pinned code and enrollment input verified')
'''
                expected = json.loads(package(row))
                expected["snapshot"] = base64.b64encode(pinned).decode()
                PHASE = "verify_staged_code_and_snapshot"
                run_remote(command, shlex.join(["python3", "-c", check]), json.dumps(expected).encode(), env)
                argv = ["python3", "-B", PREFIX + "/enroll_netplan.py", "apply",
                        "--service-id", row["service_id"], "--ip", row["ip_at_enrollment"],
                        "--package-root", PREFIX, "--snapshot", PREFIX + "/enrollment-snapshot.json",
                        "--legacy-ip", alias]
                PHASE = "enroll_guest"
                result = run_remote(command, shlex.join(argv), b"", env)
                receipt = json.loads(result)
                assert receipt["ok"] is True and receipt["ready"] is True
                assert receipt["network_restarted"] is False and receipt["vpn_restarted"] is False
                name = row["key"] + ".enrolled-" + hashlib.sha256(result).hexdigest()[:16] + ".json"
                access.private_write(output / name, result)
                print(result.decode().strip())
                return
            PHASE = "stage_code"
            staged = run_remote(command, shlex.join(["python3", "-c", REMOTE_INSTALL]), package(row), env)
            assert json.loads(staged)["staged"] is True
            snapshot_argv = ["python3", "-B", PREFIX + "/enroll_netplan.py", "snapshot", "--service-id", row["service_id"],
                             "--ip", row["ip_at_enrollment"], "--package-root", PREFIX]
            snapshot = run_remote(command, shlex.join(snapshot_argv), b"", env)
            obj = json.loads(snapshot)
            assert obj["service_id"] == row["service_id"] and obj["ip"] == row["ip_at_enrollment"]
            access.private_write(output / (row["key"] + ".enrollment.json"), snapshot)
            # Root-private input is an operational artifact, not the live YAML.
            # Preserve a prior pinned snapshot rather than silently refreshing it.
            check_or_save = "import os,sys; p=" + repr(PREFIX + "/enrollment-snapshot.json") + "; raw=sys.stdin.buffer.read(1000001); assert 0<len(raw)<1000000; "
            check_or_save += "\nif os.path.exists(p):\n import stat\n m=os.lstat(p); assert stat.S_ISREG(m.st_mode) and m.st_uid==0 and stat.S_IMODE(m.st_mode)==0o600 and open(p,'rb').read()==raw\nelse:\n fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)\n with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())"
            run_remote(command, shlex.join(["python3", "-c", check_or_save]), snapshot, env)
            plan_argv = snapshot_argv.copy()
            plan_argv[3] = "plan"
            plan_argv += ["--snapshot", PREFIX + "/enrollment-snapshot.json", "--legacy-ip", alias]
            plan = run_remote(command, shlex.join(plan_argv), b"", env)
            print(plan.decode().strip())
        finally:
            del secret
            env.pop("SPN_GUEST_SETUP_PASSWORD", None)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print("Staging not confirmed [" + PHASE + "]: " + message, file=sys.stderr)
        raise SystemExit(1)
