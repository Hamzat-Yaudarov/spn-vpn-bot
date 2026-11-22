"""Explicit activation of the already prepared owner-only 10 RUB entry pilot.

Default is a pure plan. Only --activate reads protected files, performs bounded
read-only merchant/DB checks, replaces the master configuration and starts the
two fixed franchise services. No invoices, webhooks, messages or DDL are issued.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import sys
import time
import urllib.request


TARGET = "92.42.102.103"
RELEASE = Path("/opt/spn-franchise-staging/releases/20261022-2186dc35d20e")
PRIVATE = Path("/etc/spn-partner-private")
CONFIG = PRIVATE / "staging.json"
PENDING = PRIVATE / "staging.owner-entry-pilot.pending.json"
BACKUP = PRIVATE / "staging.before-owner-entry-pilot.json"
DATA_BACKUP = PRIVATE / "before-owner-entry-pilot-20261022.json"
DATA_SHA256 = "914f1eaa370c2d6785d5933ba7122e78b61e14db1471f02742aa3772c48acba2"
UNITS = ("spn-partner-http.service", "spn-partner-worker.service")
UNIT_DIRECTORY = Path("/etc/systemd/system")
UNIT_SEARCH = tuple(Path(p) for p in ("/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system"))
ROOT_UID = 0
MAX_CONFIG = 65_536
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
            "PYTHON_DOTENV_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1"}


class ActivationError(ValueError):
    pass


def require(value):
    if not value:
        raise ActivationError("Owner-only pilot activation prerequisite failed; private details suppressed.")


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise ActivationError("Only the fixed activation options are accepted.")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def no_symlinks(path):
    require(not any(item.is_symlink() for item in (path, *path.parents)))


def private_read(path, *, maximum=MAX_CONFIG, mode=0o600):
    no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == mode and 0 < info.st_size <= maximum)
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_mtime_ns, item.st_size, item.st_nlink, item.st_mode)
        require(len(raw) == info.st_size and identity(after) == identity(info))
        return raw
    finally:
        os.close(fd)


def command(args, *, cwd=None, timeout=90, allowed=(0,)):
    result = subprocess.run(args, cwd=cwd, env=SAFE_ENV, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    require(result.returncode in allowed)
    require(len(result.stdout) <= 131_072)
    return result.stdout.decode("utf-8").strip()


def expected_unit(kind):
    runtime = f"spn-partner-{kind}"
    action = "--serve --port 8966" if kind == "http" else "--worker"
    stop_timeout = 45 if kind == "http" else 360
    return f"""# Installed disabled; no credential values are embedded here.
[Unit]
Description=Isolated franchise staging {kind}
After=network-online.target
Wants=network-online.target
ConditionPathExists={PRIVATE}/staging.json
StartLimitIntervalSec=120
StartLimitBurst=3

[Service]
Type=simple
User=spn-partner
Group=spn-partner
WorkingDirectory={RELEASE}
Environment=PYTHON_DOTENV_DISABLED=1
Environment=PYTHONDONTWRITEBYTECODE=1
LoadCredential=staging.json:{PRIVATE}/staging.json
RuntimeDirectory={runtime}
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=no
ExecStartPre=/usr/bin/install --mode=0600 --no-target-directory ${{CREDENTIALS_DIRECTORY}}/staging.json /run/{runtime}/staging.json
ExecStart={RELEASE}/.venv/bin/python -I scripts/partner_runtime.py --config /run/{runtime}/staging.json {action}
Restart=on-failure
RestartSec=15
TimeoutStopSec={stop_timeout}
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths=/var/lib/spn-partner
RestrictSUIDSGID=true
LockPersonality=true
LimitCORE=0
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
StandardInput=null
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
""".encode()


def service_state(*, active_pilot=False):
    for name, kind in zip(UNITS, ("http", "worker")):
        require(private_read(UNIT_DIRECTORY / name, mode=0o644) == expected_unit(kind))
        require(not command(["/usr/bin/systemctl", "show", "--property=DropInPaths", "--value", name]))
        require(command(["/usr/bin/systemctl", "show", "--property=FragmentPath", "--value", name]) == str(UNIT_DIRECTORY / name))
    for name in UNITS:
        active = "active" if name == UNITS[0] or active_pilot else "inactive"
        enabled = "enabled" if name == UNITS[0] or active_pilot else "disabled"
        require(command(["/usr/bin/systemctl", "is-active", name], allowed=(0, 3, 4)) == active)
        require(command(["/usr/bin/systemctl", "is-enabled", name], allowed=(0, 1, 4)) == enabled)


def verify_rollback_backup(master_raw, *, resume_verified_rollback=False):
    require(type(resume_verified_rollback) is bool)
    exists = BACKUP.exists() or BACKUP.is_symlink()
    if resume_verified_rollback:
        require(exists and private_read(BACKUP) == master_raw)
    else:
        require(not exists)


def preflight(target, *, resume_verified_rollback=False):
    require(target == TARGET and sys.platform == "linux" and os.geteuid() == ROOT_UID)
    addresses = json.loads(command(["/usr/sbin/ip", "-j", "-4", "address", "show", "scope", "global"]))
    require(any(a.get("local") == TARGET for link in addresses for a in link.get("addr_info", [])))
    for path in (PRIVATE, RELEASE, UNIT_DIRECTORY):
        no_symlinks(path)
        info = path.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == ROOT_UID and not info.st_mode & 0o022)
    require(stat.S_IMODE(PRIVATE.stat().st_mode) == 0o700)
    for name in UNITS:
        for prefix in UNIT_SEARCH:
            for suffix in (name, name + ".d", "spn-partner-.service.d", "spn-.service.d", "service.d"):
                path = prefix / suffix
                if path == UNIT_DIRECTORY / name: continue
                require(not path.exists() and not path.is_symlink())
    data = private_read(DATA_BACKUP, maximum=10 * 1024 * 1024)
    require(hashlib.sha256(data).hexdigest() == DATA_SHA256)
    service_state()
    master_raw, pending_raw = private_read(CONFIG), private_read(PENDING)
    verify_rollback_backup(master_raw, resume_verified_rollback=resume_verified_rollback)
    return master_raw, pending_raw


def candidate_for(master_raw, pending_raw):
    master = json.loads(master_raw, object_pairs_hook=unique_object)
    pending = json.loads(pending_raw, object_pairs_hook=unique_object)
    source = RELEASE / "scripts/partner_enable_owner_pilot.py"
    # Root-owned immutable release code is loaded only after explicit preflight.
    private_read(source, mode=0o640)
    spec = importlib.util.spec_from_file_location("_owner_pilot_candidate", source)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    settings = pending["runtime"]
    rebuilt = builder.make_candidate(master, shop_id=settings["yookassa_shop_id"],
                                     secret_key=settings["yookassa_secret_key"])
    require(pending == rebuilt)
    return pending


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ActivationError("Redirect refused.")


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def check_merchant(candidate):
    runtime = candidate["runtime"]
    shop, key = runtime["yookassa_shop_id"], runtime["yookassa_secret_key"]
    auth = base64.b64encode((shop + ":" + key).encode()).decode()
    request = urllib.request.Request("https://api.yookassa.ru/v3/me", method="GET",
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"})
    with opener().open(request, timeout=12) as response:
        require(response.status == 200)
        raw = response.read(131_073)
        require(len(raw) <= 131_072)
    merchant = json.loads(raw, object_pairs_hook=unique_object)
    require(merchant.get("account_id") == shop and merchant.get("test") is False
            and merchant.get("status") == "enabled" and merchant.get("fiscalization_enabled") is False)


def check_database():
    raw = command([str(RELEASE / ".venv/bin/python"), "-I", "scripts/partner_runtime.py",
                   "--config", str(PENDING), "--probe"], cwd=RELEASE, timeout=60)
    report = json.loads(raw, object_pairs_hook=unique_object)
    require(report.get("configured") is True and report.get("database_ready") is True
            and report.get("database_checked") is True and report.get("production_ready") is False
            and not report.get("blockers"))


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    except BaseException:
        path.unlink()
        raise
    finally:
        os.close(fd)


def replace_master(raw):
    temporary = PRIVATE / ".staging.owner-entry-pilot.activate.tmp"
    write_new(temporary, raw)
    try:
        os.replace(temporary, CONFIG)
        fd = os.open(PRIVATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temporary.exists(): temporary.unlink()


def http_ready():
    deadline = time.monotonic() + 25
    request = urllib.request.Request("http://127.0.0.1:8966/partners", method="GET",
                                     headers={"Host": "partners.wayspn.online"})
    while time.monotonic() < deadline:
        try:
            with opener().open(request, timeout=2) as response:
                if response.status == 200: return
        except Exception:
            pass
        time.sleep(0.5)
    raise ActivationError("Private HTTP did not become ready.")


def rollback(master_raw, pending_raw):
    # Stop processing before restoring the exact previously disabled config.
    failures = []
    for args in (["/usr/bin/systemctl", "stop", UNITS[1]],
                 ["/usr/bin/systemctl", "disable", UNITS[1]]):
        try: command(args)
        except Exception: failures.append(True)
    current = private_read(CONFIG)
    require(current in (master_raw, pending_raw))
    require(private_read(BACKUP) == master_raw)
    if current != master_raw: replace_master(master_raw)
    command(["/usr/bin/systemctl", "restart", UNITS[0]])
    service_state()
    http_ready()
    require(not failures)


def activate(target, *, resume_verified_rollback=False):
    master_raw, pending_raw = preflight(target, resume_verified_rollback=resume_verified_rollback)
    candidate = candidate_for(master_raw, pending_raw)
    check_merchant(candidate)
    check_database()
    require(private_read(CONFIG) == master_raw and private_read(PENDING) == pending_raw)
    service_state()
    verify_rollback_backup(master_raw, resume_verified_rollback=resume_verified_rollback)
    if not resume_verified_rollback:
        write_new(BACKUP, master_raw)
    changed = False
    try:
        changed = True
        replace_master(pending_raw)
        command(["/usr/bin/systemctl", "restart", UNITS[0]])
        http_ready()
        require(private_read(CONFIG) == pending_raw)
        command(["/usr/bin/systemctl", "enable", "--now", UNITS[1]])
        service_state(active_pilot=True)
        http_ready()
    except BaseException:
        if changed: rollback(master_raw, pending_raw)
        raise
    print(json.dumps({"activated": True, "scope": "owner_entry_only", "owner_id": 6910097562,
                      "entry_rub": 10, "http": "active", "worker": "active",
                      "subscription_payments": False, "vpn_provisioning": False,
                      "invoices_created": 0, "configuration_backup": str(BACKUP)}))


def main(argv=None):
    try:
        parser = PrivateParser(description=__doc__)
        parser.add_argument("--activate", action="store_true")
        parser.add_argument("--confirm-target")
        parser.add_argument("--resume-verified-rollback", action="store_true",
                            help="Reuse only an existing exact byte-for-byte disabled-master backup")
        args = parser.parse_args(argv)
        if not args.activate:
            print(json.dumps({"plan_only": True, "target": TARGET, "release": str(RELEASE),
                              "scope": "owner_entry_10_rub_only", "secret_files_read": False,
                              "network_used": False, "services_changed": False}))
            return 0
        activate(args.confirm_target, resume_verified_rollback=args.resume_verified_rollback)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Activation interrupted; check private rollback state before retry.", file=sys.stderr)
        return 130
    except Exception:
        print("Activation stopped; raw output and private values suppressed. Check rollback state before retry.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
