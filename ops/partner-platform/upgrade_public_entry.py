"""Fixed franchise release/config transition; default is a no-I/O plan.

The only new live capability is public 10-RUB entry. Customer VPN sales,
provisioning and delivery remain disabled. DDL is a separate reviewed step.
No credential is printed, downloaded or regenerated. No invoice is created.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import types

TARGET = "92.42.102.103"
OLD_RELEASE = Path("/opt/spn-franchise-staging/releases/20261023-c2a2b42dc1d4")
PRIVATE = Path("/etc/spn-partner-private")
CONFIG = PRIVATE / "staging.json"
DATA_BACKUP = PRIVATE / "before-public-entry-20261025.json"
OWNER = 6910097562
PROJECT = "crrmaqiakrqpfvhiwtkq"
HELPER_HASHES = {
    "upgrade_bot_presentation.py": "5632e1d09189696017be1ca0b6e4abc01399636c0b0d7555542d04fb88da18cd",
    "backup_private_pilot.py": "cc18c85993b7ba56ff16c93835d4a9c02f25ed25b4a84d73d32cfb2ba3545098",
}
ALLOWED_CHANGED = frozenset({
    "services/partner_runtime.py", "services/partner_entry_pricing.py",
    "services/partner_yookassa.py", "services/partner_manager.py",
    "services/partner_bot.py", "services/partner_store.py",
    "services/partner_callback_ack.py", "services/partner_web_auth.py",
    "services/partner_management.py", "services/partner_support.py",
    "partner_client_web.py", "partner_web.py",
    "partner_manager_web.py", "partner_platform_web.py", "partner_support_web.py",
    "static/partners/index.html", "static/partners/assets/partners.js",
    "static/partners/assets/partners.css", "static/partners/assets/workspace.js",
    "static/partners/support.html", "static/partners/assets/support.js",
    "ops/partner-platform/public_entry_trial.sql",
})


class UpgradeError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostics echo supplied arguments, which might
        # accidentally contain private values. Only report a fixed message.
        raise UpgradeError("Only fixed public-entry upgrade options are accepted.")


def require(value):
    if not value:
        raise UpgradeError("Public entry transition prerequisites failed.")


def load_helper(name):
    path = Path(__file__).with_name(name)
    # The imported presentation helper supplies the full root/no-symlink check.
    import stat
    for item in (path.parent, *path.parent.parents):
        info = item.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and not info.st_mode & 0o022 and info.st_size <= 100_000)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        raw = os.read(fd, 100_001)
        after = os.fstat(fd)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                                  value.st_nlink, value.st_size, value.st_mtime_ns)
        require(identity(after) == identity(info) and len(raw) == info.st_size
                and hashlib.sha256(raw).hexdigest() == HELPER_HASHES[name])
    finally:
        os.close(fd)
    module = types.ModuleType("_public_entry_" + name.replace(".", "_"))
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def candidate(raw):
    value = json.loads(raw)
    settings = value["runtime"]
    require(settings.get("enable_owner_entry_pilot") is True
            and settings.get("manager_allowed_user_ids") == [OWNER]
            and settings.get("operator_ids") == [OWNER]
            and settings.get("admin_recipient_id") == OWNER
            and not settings.get("enable_public_entry_trial", False))
    value = deepcopy(value)
    value["runtime"].update(enable_owner_entry_pilot=False,
                            enable_public_entry_trial=True,
                            manager_allowed_user_ids=None)
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def load_ops():
    require(sys.platform == "linux" and os.geteuid() == 0)
    presentation = load_helper("upgrade_bot_presentation.py")
    presentation.OLD_RELEASE = OLD_RELEASE
    ops = presentation.load_ops()
    ops.OLD_RELEASE = OLD_RELEASE
    return presentation, ops


def backup_database(target):
    require(target == TARGET)
    _, ops = load_ops()
    _, raw = ops.preflight(target, OLD_RELEASE, None)
    ops.unused(DATA_BACKUP)
    snapshot = load_helper("backup_private_pilot.py")
    snapshot.EXPECTED_TABLES = 32
    snapshot.BACKUP = DATA_BACKUP
    sys.path.insert(0, str(OLD_RELEASE))
    from scripts.partner_runtime import load_config, open_pool
    settings, _ = load_config(CONFIG)
    require(settings.database_project_ref == PROJECT)

    async def perform():
        async with open_pool(settings) as pool:
            return await snapshot.snapshot(pool)
    blob, counts = asyncio.run(asyncio.wait_for(perform(), timeout=150))
    ops.unchanged(raw)
    snapshot.write_exclusive(blob)
    return {"backup": str(DATA_BACKUP), "tables": len(counts), "rows": sum(counts.values()),
            "sha256": hashlib.sha256(blob).hexdigest(), "database_writes": 0}


def release_diff(ops, files):
    old = ops.document(ops.read_regular(OLD_RELEASE / "PACKAGE_MANIFEST.json", mode=0o640,
                                      maximum=5_000_000))
    new = ops.document(files["PACKAGE_MANIFEST.json"])
    before, after = old["files"], new["files"]
    require(old.get("format") == new.get("format") == 1
            and old.get("contains_credentials") is new.get("contains_credentials") is False
            and set(before) <= set(after)
            and set(after) == set(files) - {"PACKAGE_MANIFEST.json"})
    for name, entry in before.items():
        require(ops.installer.safe_member("spn-partner-staging/" + name) == name)
        raw = ops.read_regular(OLD_RELEASE / name, mode=0o640, maximum=ops.installer.MAX_FILE)
        require(entry == {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    changed = {name for name in after if before.get(name) != after[name]}
    require(changed <= ALLOWED_CHANGED and "services/partner_runtime.py" in changed
            and before[ops.installer.REQUIREMENTS] == after[ops.installer.REQUIREMENTS])
    return sorted(changed)


def upgrade(archive, sha, target, backup_sha):
    require(target == TARGET and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha))
    presentation, ops = load_ops()
    release = OLD_RELEASE.parent / ("20261025-" + sha[:12])
    gid, raw = ops.preflight(target, OLD_RELEASE, None)
    saved = ops.read_regular(DATA_BACKUP, maximum=20_000_000)
    require(isinstance(backup_sha, str) and hashlib.sha256(saved).hexdigest() == backup_sha)
    snapshot = ops.document(saved)
    require(snapshot.get("project_ref") == PROJECT and len(snapshot.get("tables", {})) == 32)
    prepared = candidate(raw)
    backup = PRIVATE / ("public-entry-before-20261025-" + sha[:12])
    pending = PRIVATE / ("public-entry-pending-20261025-" + sha[:12] + ".json")
    ops.unused(release); ops.unused(backup); ops.unused(pending)
    files = ops.installer.validate_archive(ops.read_regular(archive, mode=None,
                                         maximum=ops.installer.MAX_ARCHIVE), expected_sha256=sha)
    changed = release_diff(ops, files)
    os.umask(0o077)
    with ops.selected_release(release): ops.installer.extract_release(files, gid)
    presentation.copy_environment(ops, release, gid)
    ops.write_new(pending, prepared)
    # Both checks are read-only. The public price DDL must already be installed.
    ops.runtime(release, pending, "--check")
    ops.runtime(release, pending, "--probe")
    # The new code also supports private access on the broadened price schema.
    # The previous binary does not: never blindly roll it back after this DDL.
    ops.runtime(release, CONFIG, "--probe")
    staging = release / "deployment-units"
    staging.mkdir(mode=0o750); os.chown(staging, 0, gid)
    for name, kind in zip(ops.UNITS, ("http", "worker")):
        ops.installer.write_exclusive(staging / name, ops.render_unit(release, kind), mode=0o640, gid=gid)
    ops.command(["/usr/bin/systemd-analyze", "verify", *(str(staging / n) for n in ops.UNITS)])
    ops.unchanged(raw); ops.service_state(OLD_RELEASE)
    backup.mkdir(mode=0o700)
    ops.write_new(backup / "staging.json", raw)
    for name in ops.UNITS: ops.write_new(backup / name, ops.read_regular(ops.UNIT_DIRECTORY / name, mode=0o644))
    paused = False
    try:
        paused = True
        ops.command(["/usr/bin/systemctl", "stop", *ops.UNITS])
        ops.unchanged(raw)
        require(ops.read_regular(pending) == prepared)
        ops.atomic(CONFIG, prepared)
        for name in ops.UNITS:
            require(ops.read_regular(ops.UNIT_DIRECTORY / name, mode=0o644) == ops.read_regular(backup / name))
            ops.atomic(ops.UNIT_DIRECTORY / name, ops.read_regular(staging / name, mode=0o640), 0o644)
        ops.command(["/usr/bin/systemctl", "daemon-reload"])
        ops.command(["/usr/bin/systemctl", "start", ops.UNITS[0]])
        ops.http_ready()
        require(ops.read_regular(CONFIG) == prepared)
        ops.command(["/usr/bin/systemctl", "start", ops.UNITS[1]])
        ops.service_state(release)
    except BaseException:
        if paused:
            ops.command(["/usr/bin/systemctl", "stop", *ops.UNITS])
            require(ops.read_regular(CONFIG) in (raw, prepared))
            ops.atomic(CONFIG, raw)
            # Fail back to PRIVATE ACCESS with the reviewed new code. Old code
            # cannot start against public-trial DDL; restoring it is a separate
            # operator/schema operation, not an automatic financial rollback.
            for name in ops.UNITS:
                ops.atomic(ops.UNIT_DIRECTORY / name,
                           ops.read_regular(staging / name, mode=0o640), 0o644)
            ops.command(["/usr/bin/systemctl", "daemon-reload"])
            ops.command(["/usr/bin/systemctl", "start", ops.UNITS[0]])
            ops.http_ready()
            ops.command(["/usr/bin/systemctl", "start", ops.UNITS[1]])
        raise
    require(ops.read_regular(CONFIG) == prepared)
    return {"upgraded": True, "release": str(release), "changed_files": changed,
            "entry_price_rub": 10, "manager_access": "public", "rollback": str(backup),
            "customer_billing": False, "vpn_provisioning": False,
            "dependencies": "unchanged_copy", "invoices_created": 0, "services": "active"}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        actions = parser.add_mutually_exclusive_group()
        actions.add_argument("--backup", action="store_true")
        actions.add_argument("--upgrade", action="store_true")
        parser.add_argument("--confirm-target")
        parser.add_argument("--archive", type=Path)
        parser.add_argument("--sha256")
        parser.add_argument("--backup-sha256")
        args = parser.parse_args(argv)
        if args.backup:
            report = backup_database(args.confirm_target)
        elif args.upgrade:
            require(args.archive is not None)
            report = upgrade(args.archive.absolute(), args.sha256, args.confirm_target, args.backup_sha256)
        else:
            report = {"plan_only": True, "target": TARGET, "secret_files_read": False,
                      "entry_price_rub": 10, "customer_billing": False, "network_used": False}
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception:
        print("Public entry upgrade stopped; private details suppressed. Inspect retained backups before retry.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
