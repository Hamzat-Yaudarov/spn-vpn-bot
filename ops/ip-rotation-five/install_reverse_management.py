#!/usr/bin/env python3
"""Root-local, explicitly staged reverse management setup. Default: plan only.

No code in this file contacts a VPS from the Mac. A reviewed copy is run on
the intended Linux host, with bounded public-key-only JSON on stdin.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys


def trusted_source(path):
    if sys.platform.startswith("linux") and os.geteuid() == 0:
        for parent in path.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("Untrusted installer source directory")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
            raise ValueError("Untrusted installer source")


def load(name):
    path = Path(__file__).with_name(name + ".py")
    trusted_source(path)
    spec = importlib.util.spec_from_file_location("_reverse_setup_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


for _source in (__file__, str(Path(__file__).with_name("install_guest_keys.py"))):
    trusted_source(Path(_source))
model = load("reverse_management_model")
eu = load("install_eu_relays")
CONTROLLER_DEPLOYMENT = "276abbcb4f334b58ab25d9b2adc9d55c"
RF_STATE = "/var/lib/spn-reverse-management-controller"
GUEST_STATE = "/var/lib/spn-reverse-management-guest"
CLIENT_KEY = model.GUEST_HOME + "/.ssh/tunnel_ed25519"
CLIENT_KNOWN = model.GUEST_HOME + "/.ssh/controller_known_hosts"
CLIENT_UNIT = "/etc/systemd/system/" + model.GUEST_UNIT


def need(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def worker(role):
    value = eu.worker_namespace()
    value.update(SERVICES=set(model.SERVICE_PORTS),
                 USER=model.RF_USER if role == "rf" else model.GUEST_USER,
                 HOME=model.RF_HOME if role == "rf" else model.GUEST_HOME)
    if role == "rf":
        value.update(POLICY=model.controller_policy(), DROPIN=model.RF_DROPIN,
                     EXPECTED=model.RF_EXPECTED)
    return value


def public_read(files, path):
    info = path.lstat()
    need(stat.S_IMODE(info.st_mode) in (0o600, 0o644), "Unexpected public-key file mode")
    return files.read(path, mode=stat.S_IMODE(info.st_mode))[0]


def guest_identity(w, fs, service, check=True):
    prefix = fs.path("/opt/spn-ip-failover-guest")
    fs.require_directory(prefix)
    raw, _ = fs.read(prefix / ".service-id")
    need(raw == (service + "\n").encode(), "Guest helper belongs to another hosting service")
    if check:
        result = w["subprocess"].run(
            ["/usr/bin/python3", "-I", str(prefix / "guest_rpc.py")],
            input=encoded({"operation": "check", "server_id": service}),
            stdout=w["subprocess"].PIPE, stderr=w["subprocess"].PIPE, timeout=90, check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LC_ALL": "C"},
        )
        need(result.returncode == 0 and len(result.stdout) < 1024
             and json.loads(result.stdout) == {"ok": True}, "Guest network journal is not ready")


def own_account(w, fs, directory, accounts, commands):
    """Foreign users never get adopted, including ambiguous interrupted useradd."""
    return w["ensure_account"](fs, directory, accounts, commands)


def setup_lock(w, fs, directory):
    fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == fs.uid and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, "Unsafe reverse-management setup lock")
        w["fcntl"].flock(fd, w["fcntl"].LOCK_EX | w["fcntl"].LOCK_NB)
        return fd
    except Exception:
        os.close(fd)
        raise


def guest_prepare(request, *, w=None, fs=None, accounts=None, commands=None, readiness=True):
    need(isinstance(request, dict) and set(request) == {"service_id", "apply"} and request["apply"] is True,
         "Invalid guest identity preparation request")
    service = request["service_id"]
    model.service_port(service)
    w = w or worker("guest")
    fs, commands, accounts = fs or w["Files"](), commands or w["Commands"](), accounts or w["Accounts"]()
    guest_identity(w, fs, service, check=readiness)
    directory = fs.path(GUEST_STATE)
    fs.directory(directory)
    fs.once(directory / ".service-id", (service + "\n").encode())
    lock = setup_lock(w, fs, directory)
    try:
        identity = own_account(w, fs, directory, accounts, commands)
        uf = w["UserFiles"](fs.root, identity["uid"], identity["gid"], admin_uid=fs.uid)
        home = uf.path(model.GUEST_HOME)
        uf.directory(home)
        uf.directory(home / ".ssh")
        key, pub = uf.path(CLIENT_KEY), uf.path(CLIENT_KEY + ".pub")
        intent = {"format": 1, "service_id": service, "private_key_path": CLIENT_KEY,
                  "private_key_exported": False, "owner_uid": identity["uid"]}
        fs.once(directory / "key-intent.json", encoded(intent))
        ready, _ = fs.read(directory / "key-ready.json", absent=True)
        need(ready is None or key.exists() and pub.exists(),
             "Previously prepared client key was removed; no key will be regenerated")
        if not key.exists() and not pub.exists():
            commands.run(["/usr/sbin/runuser", "-u", model.GUEST_USER, "--", "/usr/bin/ssh-keygen",
                          "-q", "-t", "ed25519", "-N", "", "-C", "spn-reverse:" + service, "-f", str(key)])
        info = key.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == identity["uid"] and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, "Private client key has unexpected ownership")
        # The external tool reads the private key only on this guest and emits
        # its public half. No private bytes enter the installer or its output.
        derived = model.public_key(commands.run(
            ["/usr/sbin/runuser", "-u", model.GUEST_USER, "--", "/usr/bin/ssh-keygen", "-y", "-f", str(key)]
        ).decode().strip())
        if not pub.exists():
            uf.replace(pub, None, None, (derived + "\n").encode())
        need(model.public_key(public_read(uf, pub).decode().strip()) == derived,
             "Public/private client key halves do not match")
        fs.once(directory / "key-ready.json", encoded({**intent, "public_key_sha256": sha(derived.encode())}))
        return {"prepared": True, "service_id": service, "port": model.service_port(service),
                "public_key": derived, "private_key_exported": False, "client_started": False}
    finally:
        os.close(lock)


def registry_read(fs, path):
    raw, meta = fs.read(path, absent=True)
    value = {"format": 1, "services": {}} if raw is None else json.loads(raw)
    need(isinstance(value, dict) and set(value) == {"format", "services"} and value["format"] == 1
         and isinstance(value["services"], dict) and set(value["services"]) <= set(model.SERVICE_PORTS),
         "Invalid reverse-management registry")
    seen = set()
    for service, row in value["services"].items():
        need(isinstance(row, dict) and set(row) == {"public_key", "port"}
             and row["port"] == model.service_port(service), "Registry port changed ownership")
        key = model.public_key(row["public_key"])
        need(key == row["public_key"] and key not in seen, "Registry public keys must be distinct")
        seen.add(key)
    return value, raw, meta


def registry_authorized(value):
    return b"".join(model.rf_key_line(service, row["public_key"])
                    for service, row in sorted(value["services"].items()))


def rf_register(request, *, w=None, fs=None, accounts=None, commands=None):
    need(isinstance(request, dict) and set(request) == {"service_id", "public_key", "apply"}
         and request["apply"] is True, "Invalid RF key registration request")
    service, key = request["service_id"], model.public_key(request["public_key"])
    port = model.service_port(service)
    w = w or worker("rf")
    fs, commands, accounts = fs or w["Files"](), commands or w["Commands"](), accounts or w["Accounts"]()
    marker, _ = fs.read(fs.path("/opt/wayspn/.controller-deployment"))
    need(marker.decode().strip() == CONTROLLER_DEPLOYMENT, "Wrong Russian controller deployment")
    directory = fs.path(RF_STATE)
    fs.directory(directory)
    lock = setup_lock(w, fs, directory)
    try:
        commands.syntax()
        commands.service_ready()
        baseline = commands.effective("root", model.CONTROLLER)
        dropin = fs.path(model.RF_DROPIN)
        old_policy, _ = w["policy_file"](fs, dropin)
        policy = model.controller_policy()
        need(old_policy is None or old_policy == policy, "RF management drop-in is foreign")
        loaded, _ = fs.read(directory / "policy-loaded.json", absent=True)
        need(loaded is None or old_policy == policy, "Previously loaded RF SSH policy was removed")
        identity = own_account(w, fs, directory, accounts, commands)
        uf = w["UserFiles"](fs.root, identity["uid"], identity["gid"], admin_uid=fs.uid)
        home = uf.path(model.RF_HOME)
        uf.directory(home)
        uf.directory(home / ".ssh")
        auth = home / ".ssh/authorized_keys"
        state_path = directory / "registry.json"
        registry, before_registry, registry_meta = registry_read(fs, state_path)
        existing = registry["services"].get(service)
        need(existing is None or existing == {"public_key": key, "port": port},
             "Service already owns another management key")
        need(not any(row["public_key"] == key and sid != service for sid, row in registry["services"].items()),
             "A client key cannot control another guest's listener")
        before_auth, auth_meta = uf.read(auth, absent=True)
        expected_before = registry_authorized(registry)
        after_registry = json.loads(json.dumps(registry))
        after_registry["services"][service] = {"public_key": key, "port": port}
        after_auth = registry_authorized(after_registry)
        transaction = directory / service
        fs.directory(transaction)
        for other in model.SERVICE_PORTS:
            if other == service:
                continue
            pending_dir = directory / other
            if not pending_dir.exists():
                continue
            pending, _ = fs.read(pending_dir / "intent.json", absent=True)
            done, _ = fs.read(pending_dir / "complete.json", absent=True)
            need(pending is None or done is not None, "Another RF registration is pending")
        old_intent, _ = fs.read(transaction / "intent.json", absent=True)
        # A lost reply can leave the authorized key committed before registry.
        if before_auth not in (expected_before, None if not expected_before else expected_before):
            need(old_intent is not None and before_auth == after_auth,
                 "Dedicated tunnel authorized_keys changed outside this registry")
            prior = json.loads(old_intent)
            need(prior.get("service_id") == service and prior.get("key_sha256") == sha(key.encode())
                 and prior.get("after_auth_sha256") == sha(after_auth),
                 "Pending RF authorization belongs to another request")
        if existing is not None:
            need(old_intent is not None, "Existing RF registry entry has no owned setup intent")
            prior = json.loads(old_intent)
            need(prior.get("format") == 1 and prior.get("service_id") == service
                 and prior.get("key_sha256") == sha(key.encode())
                 and prior.get("policy_sha256") == sha(policy), "Owned RF setup intent identifies another request")
        if existing is None:
            intent = {"format": 1, "service_id": service, "key_sha256": sha(key.encode()),
                      "before_auth_sha256": sha(expected_before), "after_auth_sha256": sha(after_auth),
                      "before_registry": registry, "after_registry": after_registry, "policy_sha256": sha(policy)}
            fs.once(transaction / "intent.json", encoded(intent))
            backup, _ = fs.read(transaction / "authorized_keys.before.json", absent=True)
            if backup is None:
                fs.once(transaction / "authorized_keys.before.json", encoded(
                    {"was_absent": before_auth is None, "base64": base64.b64encode(before_auth or b"").decode()}))
        backup, _ = fs.read(directory / "ssh-policy.before.json", absent=True)
        if backup is None:
            fs.once(directory / "ssh-policy.before.json", encoded(
                {"was_absent": old_policy is None, "base64": base64.b64encode(old_policy or b"").decode()}))
        created = old_policy is None
        if created:
            fs.once(dropin, policy)
        try:
            commands.syntax()
            need(commands.effective("root", model.CONTROLLER) == baseline,
                 "Root SSH policy changed; reload is forbidden")
            w["restrictions"](commands.effective(model.RF_USER, model.CONTROLLER))
        except Exception:
            if created:
                current, _ = w["policy_file"](fs, dropin)
                need(current == policy, "New RF policy changed before rollback")
                dropin.unlink()
                fs.sync(dropin.parent)
                commands.syntax()
                need(commands.effective("root", model.CONTROLLER) == baseline, "Root policy rollback unconfirmed")
            raise
        loaded_value = encoded({"format": 1, "policy_sha256": sha(policy)})
        if loaded is None:
            commands.reload()
            need(commands.effective("root", model.CONTROLLER) == baseline, "Root SSH policy drifted after reload")
            w["restrictions"](commands.effective(model.RF_USER, model.CONTROLLER))
            fs.once(directory / "policy-loaded.json", loaded_value)
        else:
            need(loaded == loaded_value, "Loaded RF policy belongs to another configuration")
        if before_auth != after_auth:
            uf.replace(auth, before_auth, auth_meta, after_auth)
        if before_registry != encoded(after_registry):
            fs.replace(state_path, before_registry, registry_meta, encoded(after_registry))
        need(uf.read(auth)[0] == registry_authorized(registry_read(fs, state_path)[0]),
             "RF tunnel registry/authorized_keys readback differs")
        completed, _ = fs.read(transaction / "complete.json", absent=True)
        if completed is None:
            fs.once(transaction / "complete.json", encoded(
                {"format": 1, "service_id": service, "key_sha256": sha(key.encode()),
                 "after_auth_sha256": sha(after_auth)}))
        host_key = model.public_key(public_read(fs, fs.path("/etc/ssh/ssh_host_ed25519_key.pub")).decode().strip())
        return {"registered": True, "service_id": service, "controller": model.CONTROLLER, "port": port,
                "client_public_key_sha256": sha(key.encode()), "controller_host_key": host_key,
                "private_key_exported": False, "ssh_reload_only": True, "root_policy_unchanged": True}
    finally:
        os.close(lock)


def guest_activate(request, *, w=None, fs=None, accounts=None, commands=None, readiness=True):
    need(isinstance(request, dict) and set(request) == {"service_id", "controller_rpc_public_key", "registration", "apply"}
         and request["apply"] is True, "Invalid guest activation request")
    service, registration = request["service_id"], request["registration"]
    port = model.service_port(service)
    need(isinstance(registration, dict) and registration.get("registered") is True
         and registration.get("service_id") == service and registration.get("controller") == model.CONTROLLER
         and registration.get("port") == port, "RF registration belongs to another guest/listener")
    rpc_key = model.public_key(request["controller_rpc_public_key"])
    host_key = model.public_key(registration.get("controller_host_key"))
    w = w or worker("guest")
    fs, commands, accounts = fs or w["Files"](), commands or w["Commands"](), accounts or w["Accounts"]()
    guest_identity(w, fs, service, check=readiness)
    directory = fs.path(GUEST_STATE)
    fs.require_directory(directory)
    lock = setup_lock(w, fs, directory)
    try:
        marker, _ = fs.read(directory / ".service-id")
        need(marker == (service + "\n").encode(), "Client identity belongs to another guest")
        identity = own_account(w, fs, directory, accounts, commands)
        uf = w["UserFiles"](fs.root, identity["uid"], identity["gid"], admin_uid=fs.uid)
        public = model.public_key(public_read(uf, uf.path(CLIENT_KEY + ".pub")).decode().strip())
        need(public != rpc_key and registration.get("client_public_key_sha256") == sha(public.encode()),
             "RF registration does not bind this guest's own client public key")
        known = uf.path(CLIENT_KNOWN)
        before_known, known_meta = uf.read(known, absent=True)
        after_known = ("spn-ip-rotation-controller " + host_key + "\n").encode()
        need(before_known is None or before_known == after_known, "Controller host key changed; no automatic replacement")
        unit = fs.path(CLIENT_UNIT)
        before_unit, _ = w["policy_file"](fs, unit)
        after_unit = model.guest_unit(service)
        need(before_unit is None or before_unit == after_unit, "Client systemd unit is foreign")
        auth = fs.path("/root/.ssh/authorized_keys")
        before_auth, auth_meta = fs.read(auth)
        after_auth, changed = model.rpc_key_upgrade(before_auth, rpc_key, service)
        intent = {"format": 1, "service_id": service, "port": port, "client_public_key_sha256": sha(public.encode()),
                  "controller_host_key_sha256": sha(host_key.encode()), "rpc_key_sha256": sha(rpc_key.encode()),
                  "unit_sha256": sha(after_unit)}
        fs.once(directory / "activate-intent.json", encoded(intent))
        complete, _ = fs.read(directory / "complete.json", absent=True)
        if complete is not None:
            need(json.loads(complete) == intent and not changed and before_known == after_known
                 and before_unit == after_unit, "Completed reverse management was changed; manual review required")
        if changed:
            fs.once(directory / "rpc-authorized_keys.before.json", encoded(
                {"base64": base64.b64encode(before_auth).decode(), "after_sha256": sha(after_auth)}))
        backup, _ = fs.read(directory / "unit.before.json", absent=True)
        if backup is None:
            fs.once(directory / "unit.before.json", encoded(
                {"was_absent": before_unit is None, "base64": base64.b64encode(before_unit or b"").decode()}))
        if before_known is None:
            uf.replace(known, before_known, known_meta, after_known)
        if before_unit is None:
            fs.once(unit, after_unit)
        commands.run(["/usr/bin/systemd-analyze", "verify", str(unit)])
        if changed:
            fs.replace(auth, before_auth, auth_meta, after_auth)
        if complete is None:
            commands.run(["/usr/bin/systemctl", "daemon-reload"])
            commands.run(["/usr/bin/systemctl", "enable", "--now", model.GUEST_UNIT])
            fs.once(directory / "complete.json", encoded(intent))
        return {"configured": True, "service_id": service, "controller": model.CONTROLLER, "port": port,
                "private_key_exported": False, "network_restarted": False, "vpn_restarted": False,
                "tunnel_verified": False, "verification_required": "RF loopback SSH host-key and fixed RPC check"}
    finally:
        os.close(lock)


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "Duplicate request field")
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("guest-prepare", "rf-register", "guest-activate", "gateway-plan"), required=True)
    parser.add_argument("--service", choices=tuple(model.SERVICE_PORTS), required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        report = {"plan_only": True, "role": args.role, "service_id": args.service,
                  "controller": model.CONTROLLER, "loopback_port": model.service_port(args.service),
                  "network_used": False, "credentials_read": False, "remote_writes": False}
        if args.role == "gateway-plan":
            report["guest_ssh"] = model.gateway_ssh(args.service)
        print(json.dumps(report))
        return
    need(sys.platform.startswith("linux") and os.geteuid() == 0, "Apply only on the intended Linux host as root")
    need(args.role != "gateway-plan", "Gateway configuration publication is a separate reviewed operation")
    raw = sys.stdin.buffer.read(16385)
    need(0 < len(raw) <= 16384, "Public-key setup request is missing or oversized")
    request = json.loads(raw, object_pairs_hook=unique)
    need(request.get("service_id") == args.service, "CLI and request identify different guests")
    action = {"guest-prepare": guest_prepare, "rf-register": rf_register, "guest-activate": guest_activate}[args.role]
    print(json.dumps(action(request)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Reverse management setup stopped: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
