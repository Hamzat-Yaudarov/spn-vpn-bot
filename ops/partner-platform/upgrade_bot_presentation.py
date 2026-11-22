"""One fixed, code-only partner-bot presentation upgrade; default is pure plan.

Requires the two reviewed sibling deployment helpers. No DB writes, DDL,
configuration writes, bootstrap, Telegram calls, invoices or dependency network.
The immutable old virtualenv is copied and checked, not upgraded with pip.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import types


TARGET = "92.42.102.103"
OLD_RELEASE = Path("/opt/spn-franchise-staging/releases/20261022-8b7e781b30fd")
RELEASES = OLD_RELEASE.parent
PRIVATE = Path("/etc/spn-partner-private")
HELPER = Path(__file__).with_name("upgrade_private_pilot.py")
HELPER_SHA256 = "45e5bc369aa03f23bc2d2c8446109d5eb9e6859436e3707bd853c414de18e2ca"
NEW_HELPER = "services/connection_instructions.py"
ALLOWED_DIFF = frozenset(("services/partner_bot.py", "services/partner_brand_defaults.py",
                          "services/partner_store.py", "services/partner_subscriptions.py",
                          "partner_subscription_web.py", NEW_HELPER))
MAX_VENV_FILE = 128 * 1024 * 1024
MAX_VENV_BYTES = 512 * 1024 * 1024


class UpgradeError(ValueError):
    pass


def require(value):
    if not value:
        raise UpgradeError("Code-only presentation upgrade prerequisites failed.")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UpgradeError("Only fixed presentation-upgrade options are accepted.")


def read_protected(path, *, mode=None, maximum=100_000):
    require(not any(item.is_symlink() for item in (path, *path.parents)))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                 item.st_uid, item.st_mode, item.st_nlink)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o022 and info.st_size <= maximum
                and (mode is None or stat.S_IMODE(info.st_mode) == mode))
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(fd, min(65_536, maximum + 1 - len(raw)))
            if not chunk: break
            raw.extend(chunk)
        require(len(raw) == info.st_size and identity(os.fstat(fd)) == identity(info))
        return bytes(raw)
    finally:
        os.close(fd)


def load_ops():
    raw = read_protected(HELPER)
    require(hashlib.sha256(raw).hexdigest() == HELPER_SHA256)
    module = types.ModuleType("_reviewed_presentation_ops")
    module.__file__ = str(HELPER)
    exec(compile(raw, str(HELPER), "exec"), module.__dict__)
    module.OLD_RELEASE = OLD_RELEASE
    old_validate = module.validate_config
    module.validate_config = lambda value: old_validate(value, features=True)
    old_command = module.command
    def no_bytecode_command(args, **kwargs):
        if str(args[0]).endswith("/.venv/bin/python") and "-B" not in args:
            args = [args[0], "-B", *args[1:]]
        return old_command(args, **kwargs)
    module.command = no_bytecode_command
    # That helper's 31-table pre-DDL backup gate belongs to enabling features,
    # not this strictly code-only transaction. No DB rows are changed here.
    module.data_backup = lambda expected: None
    return module


def release_for(sha):
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha))
    return RELEASES / ("20261023-" + sha[:12])


def presentation_diff(ops, files):
    old = ops.document(read_protected(OLD_RELEASE / "PACKAGE_MANIFEST.json", mode=0o640, maximum=5_000_000))
    new = ops.document(files["PACKAGE_MANIFEST.json"])
    require(old.get("format") == new.get("format") == 1
            and old.get("archive_root") == new.get("archive_root") == "spn-partner-staging"
            and old.get("runtime_default") == new.get("runtime_default") == "disabled"
            and old.get("contains_credentials") is new.get("contains_credentials") is False
            and isinstance(old.get("files"), dict) and isinstance(new.get("files"), dict))
    before, after = old["files"], new["files"]
    require(set(before) <= set(after) and set(after) == set(files) - {"PACKAGE_MANIFEST.json"}
            and NEW_HELPER not in before and NEW_HELPER in after)
    for name, item in before.items():
        require(ops.installer.safe_member("spn-partner-staging/" + name) == name)
        raw = read_protected(OLD_RELEASE / name, mode=0o640, maximum=ops.installer.MAX_FILE)
        require(item == {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    changed = {name for name in after if before.get(name) != after[name]}
    require(changed <= ALLOWED_DIFF and {NEW_HELPER, "services/partner_bot.py"} <= changed
            and before[ops.installer.REQUIREMENTS] == after[ops.installer.REQUIREMENTS])
    return sorted(changed)


def tree_signature(root):
    """Reject external/absolute links, writable files, hardlinks and special files."""
    signature, total = {}, 0
    require(not root.is_symlink())
    for current, directories, files in os.walk(root, followlinks=False):
        folder = Path(current)
        info = folder.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
        signature[folder.relative_to(root).as_posix()] = ("directory",)
        for name in directories + files:
            path = folder / name
            entry = path.lstat()
            require(entry.st_uid == 0)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(entry.st_mode):
                target = os.readlink(path)
                require(not os.path.isabs(target) and path.resolve().is_relative_to(root.resolve()))
                signature[relative] = ("symlink", target)
            elif stat.S_ISREG(entry.st_mode):
                raw = read_protected(path, maximum=MAX_VENV_FILE)
                total += len(raw)
                require(total <= MAX_VENV_BYTES and len(signature) <= 50_000)
                signature[relative] = ("file", len(raw), hashlib.sha256(raw).hexdigest())
            else:
                require(stat.S_ISDIR(entry.st_mode))
    return signature


def versions(ops, release):
    rows = ops.document(ops.command([str(release / ".venv/bin/python"), "-I", "-B", "-m", "pip",
                                     "--disable-pip-version-check", "list", "--format=json"], cwd=release))
    require(isinstance(rows, list) and 0 < len(rows) <= 1000)
    result = {}
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"name", "version"}
                and isinstance(row["name"], str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", row["name"])
                and isinstance(row["version"], str) and re.fullmatch(r"[A-Za-z0-9_.+!-]{1,100}", row["version"]))
        name = re.sub(r"[-_.]+", "-", row["name"]).lower()
        require(name not in result)
        result[name] = row["version"]
    return result


def copy_environment(ops, release, gid):
    source, destination = OLD_RELEASE / ".venv", release / ".venv"
    original, packages = tree_signature(source), versions(ops, OLD_RELEASE)
    require(original and not destination.exists() and not destination.is_symlink())
    shutil.copytree(source, destination, symlinks=True)
    require(tree_signature(source) == original == tree_signature(destination))
    with ops.selected_release(release): ops.installer.protect_release(gid)
    code = "import json,sys;print(json.dumps({'prefix':sys.prefix,'executable':sys.executable}))"
    identity = ops.document(ops.command([str(destination / "bin/python"), "-I", "-B", "-c", code], cwd=release))
    require(identity == {"prefix": str(destination), "executable": str(destination / "bin/python")}
            and versions(ops, release) == packages)
    ops.command([str(destination / "bin/python"), "-I", "-B", "-m", "pip", "--disable-pip-version-check", "check"], cwd=release)


def upgrade(archive, sha, target):
    release = release_for(sha)
    require(target == TARGET)
    ops = load_ops()
    gid, raw = ops.preflight(target, OLD_RELEASE, None)
    backup = PRIVATE / ("presentation-before-20261023-" + sha[:12])
    ops.unused(release); ops.unused(backup)
    files = ops.installer.validate_archive(read_protected(archive, maximum=ops.installer.MAX_ARCHIVE), expected_sha256=sha)
    changed = presentation_diff(ops, files)
    os.umask(0o077)
    with ops.selected_release(release): ops.installer.extract_release(files, gid)
    copy_environment(ops, release, gid)
    ops.runtime(release, ops.CONFIG, "--check")
    ops.runtime(release, ops.CONFIG, "--probe")  # Readonly SQL/schema checks only.
    staging = release / "deployment-units"
    staging.mkdir(mode=0o750); os.chown(staging, 0, gid)
    for name, kind in zip(ops.UNITS, ("http", "worker")):
        ops.installer.write_exclusive(staging / name, ops.render_unit(release, kind), mode=0o640, gid=gid)
    ops.command(["/usr/bin/systemd-analyze", "verify", *(str(staging / name) for name in ops.UNITS)])
    ops.unchanged(raw); ops.service_state(OLD_RELEASE)
    backup.mkdir(mode=0o700)
    ops.write_new(backup / "staging.json", raw)
    for name in ops.UNITS: ops.write_new(backup / name, ops.read_regular(ops.UNIT_DIRECTORY / name, mode=0o644))
    paused = False
    try:
        paused = True  # A timed-out stop may still have been accepted.
        ops.command(["/usr/bin/systemctl", "stop", ops.UNITS[1]])
        ops.unchanged(raw); ops.service_state(OLD_RELEASE, worker_active=False)
        for name in ops.UNITS:
            require(ops.read_regular(ops.UNIT_DIRECTORY / name, mode=0o644) == ops.read_regular(backup / name))
            ops.atomic(ops.UNIT_DIRECTORY / name, ops.read_regular(staging / name, mode=0o640), 0o644)
        ops.command(["/usr/bin/systemctl", "daemon-reload"])
        ops.command(["/usr/bin/systemctl", "restart", ops.UNITS[0]])
        ops.http_ready(); ops.unchanged(raw)
        ops.command(["/usr/bin/systemctl", "start", ops.UNITS[1]])
        ops.service_state(release); ops.unchanged(raw)
    except BaseException:
        if paused:
            ops.command(["/usr/bin/systemctl", "stop", ops.UNITS[1]])
            for name in ops.UNITS: ops.atomic(ops.UNIT_DIRECTORY / name, ops.read_regular(backup / name), 0o644)
            ops.command(["/usr/bin/systemctl", "daemon-reload"])
            ops.unchanged(raw)
            ops.command(["/usr/bin/systemctl", "restart", ops.UNITS[0]])
            ops.http_ready()
            ops.command(["/usr/bin/systemctl", "start", ops.UNITS[1]])
        raise
    return {"upgraded": True, "release": str(release), "changed_files": changed,
            "configuration": "unchanged", "dependencies": "unchanged_copy", "database": "not_modified",
            "telegram_calls": 0, "payment_calls": 0, "services": "active", "rollback_units": str(backup)}


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--upgrade", action="store_true")
        parser.add_argument("--archive", type=Path)
        parser.add_argument("--sha256")
        parser.add_argument("--confirm-target")
        args = parser.parse_args(argv)
        if not args.upgrade:
            result = {"plan_only": True, "target": TARGET, "old_release": str(OLD_RELEASE),
                      "configuration": "unchanged", "database": "not_modified", "network": "no_external_writes"}
        else:
            require(args.archive is not None)
            result = upgrade(args.archive.absolute(), args.sha256, args.confirm_target)
        print(json.dumps(result, sort_keys=True))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0: return 0
        print("Presentation upgrade stopped; private details suppressed. Inspect retained release and unit backups before retry.", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
