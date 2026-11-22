"""Explicit, fixed-target upgrade of the disabled-sales private franchise stand.

Default is a pure plan. No SSH, DDL, token generation, config writes or payment
enablement. A failed new release is retained; only existing units are restored.
Ship beside the reviewed install_staging.py, whose archive and unit helpers are
reused. The archive digest is an explicit operator approval, not discovery.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys


_spec = importlib.util.spec_from_file_location(
    "_partner_upgrade_installer", Path(__file__).with_name("install_staging.py")
)
installer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(installer)
UpgradeError = installer.InstallError
TARGET = installer.TARGET
OLD_RELEASE = Path("/opt/spn-franchise-staging/releases/20261022-0484f58e1b58")
PRIVATE = installer.PRIVATE
UNITS = installer.UNITS
UNIT_DIRECTORY = installer.UNIT_DIRECTORY
OWNER = 6910097562
CONFIG = PRIVATE / "staging.json"
OFF_FLAGS = (
    "enable_billing", "enable_owner_entry_pilot", "invoice_intents_enabled",
    "panel_identity_enabled", "enable_customer_webhooks", "enable_provisioning",
    "enable_launches", "enable_notifications", "enable_marketing_delivery",
    "enable_support_delivery", "enable_finance_notifications", "enable_public_gateway",
    "enable_public_checkout", "enable_worker",
)


def require(value, message):
    if not value:
        raise UpgradeError(message)


def release_for(digest):
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "An exact lowercase SHA-256 digest is required.")
    return installer.RELEASES / ("20261022-" + digest[:12])


@contextmanager
def selected_release(release):
    previous = installer.RELEASE
    installer.RELEASE = release
    try:
        yield
    finally:
        installer.RELEASE = previous


def render_unit(release, kind):
    with selected_release(release):
        return installer.render_unit(kind).encode()


def read_regular(path, *, mode=None, maximum=65_536):
    installer.no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o022 and info.st_size <= maximum
                and (mode is None or stat.S_IMODE(info.st_mode) == mode),
                "Expected a bounded, root-owned protected regular file.")
        value = stream.read(maximum + 1)
        require(len(value) <= maximum, "Protected file exceeds its size bound.")
        return value


def validate_config(raw):
    try:
        document = json.loads(raw, object_pairs_hook=installer.unique_object)
        require(isinstance(document, dict) and set(document) <= {"runtime", "staging_addon_catalog"},
                "Unexpected private configuration envelope.")
        settings = document["runtime"]
        require(isinstance(settings, dict) and settings.get("mode") == "staging"
                and settings.get("enable_http") is True
                and settings.get("enable_manager_webhook") is True
                and settings.get("manager_allowed_user_ids") == [OWNER]
                and all(settings.get(key, False) is False for key in OFF_FLAGS),
                "Upgrade requires the existing owner-private manager with all sales and workers disabled.")
    except UpgradeError:
        raise
    except Exception:
        raise UpgradeError("Private configuration could not be validated; values suppressed.") from None


def command(args, **kwargs):
    return installer.checked_command(args, **kwargs)


def unit_state():
    for name in UNITS:
        require(not command(["/usr/bin/systemctl", "show", "--property=DropInPaths", "--value", name]),
                "Unreviewed systemd drop-ins are present.")
        require(command(["/usr/bin/systemctl", "show", "--property=FragmentPath", "--value", name])
                == str(UNIT_DIRECTORY / name), "Unexpected systemd unit source.")
    require(command(["/usr/bin/systemctl", "is-active", UNITS[0]]) == "active",
            "Existing private HTTP must be active.")
    require(command(["/usr/bin/systemctl", "is-enabled", UNITS[0]]) == "enabled",
            "Existing private HTTP enablement changed.")
    require(command(["/usr/bin/systemctl", "is-active", UNITS[1]], allowed_codes=(3, 4)) == "inactive",
            "Worker must remain inactive.")
    require(command(["/usr/bin/systemctl", "is-enabled", UNITS[1]], allowed_codes=(1, 4)) == "disabled",
            "Worker must remain disabled.")


def preflight(target, release, backup):
    require(target == TARGET and sys.platform == "linux" and os.geteuid() == 0,
            "Upgrade requires root on the explicitly confirmed staging VPS.")
    require(sys.version_info >= (3, 11), "Python 3.11 or newer is required.")
    addresses = json.loads(command(["/usr/sbin/ip", "-j", "-4", "address", "show", "scope", "global"]))
    require(any(a.get("local") == TARGET for link in addresses for a in link.get("addr_info", [])),
            "Wrong VPS address.")
    account = installer.pwd.getpwnam(installer.SERVICE_USER)
    group = installer.grp.getgrnam(installer.SERVICE_USER)
    require(account.pw_uid != 0 and account.pw_gid == group.gr_gid
            and account.pw_shell in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"},
            "Expected the existing no-login service account.")
    for path in (Path("/opt"), installer.RELEASES.parent, installer.RELEASES, UNIT_DIRECTORY, OLD_RELEASE):
        installer.directory(path, uid=0)
    installer.directory(PRIVATE, uid=0, exact_mode=0o700)
    installer.directory(installer.STATE, uid=account.pw_uid, gid=group.gr_gid, exact_mode=0o700)
    for path in (release, backup):
        require(not path.exists() and not path.is_symlink(), "New release or backup already exists; inspect before retry.")
    # Refuse inherited overrides and duplicate unit fragments; preserve only the
    # exact root-owned old unit files installed by the reviewed original tool.
    for name, kind in zip(UNITS, ("http", "worker")):
        require(read_regular(UNIT_DIRECTORY / name, mode=0o644) == render_unit(OLD_RELEASE, kind),
                "Existing unit differs from the reviewed old release.")
        for prefix in installer.UNIT_SEARCH:
            for candidate in (name, name + ".d", "spn-partner-.service.d", "spn-.service.d", "service.d"):
                path = prefix / candidate
                if path == UNIT_DIRECTORY / name:
                    continue
                require(not path.exists() and not path.is_symlink(), "Unreviewed inherited unit or override.")
    raw = read_regular(CONFIG, mode=0o600)
    validate_config(raw)
    unit_state()
    return group.gr_gid, hashlib.sha256(raw).hexdigest()


def unchanged_config(digest):
    raw = read_regular(CONFIG, mode=0o600)
    validate_config(raw)
    require(hashlib.sha256(raw).hexdigest() == digest, "Private configuration changed during upgrade.")


def atomic_unit(path, data):
    temporary = path.with_name(path.name + ".owner-upgrade-new")
    installer.write_exclusive(temporary, data, mode=0o644, gid=0)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def restore_units(backup):
    # Roll back only the two exact previous unit byte strings, never data/code
    # or credential files. Worker is neither started nor enabled.
    for name in UNITS:
        atomic_unit(UNIT_DIRECTORY / name, read_regular(backup / name, mode=0o600))
    command(["/usr/bin/systemctl", "daemon-reload"])
    command(["/usr/bin/systemctl", "restart", UNITS[0]])


def upgrade(archive, digest, target):
    release = release_for(digest)
    backup = PRIVATE / ("upgrade-before-20261022-" + digest[:12])
    gid, config_digest = preflight(target, release, backup)
    blob = read_regular(archive, maximum=installer.MAX_ARCHIVE)
    files = installer.validate_archive(blob, expected_sha256=digest)
    os.umask(0o077)
    with selected_release(release):
        installer.extract_release(files, gid)
        command([sys.executable, "-I", "-m", "venv", "--copies", str(release / ".venv")])
        python = str(release / ".venv/bin/python")
        command([python, "-I", "-m", "pip", "--disable-pip-version-check", "install",
                 "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
                 "--no-cache-dir", "--no-compile", "--require-virtualenv",
                 "-r", str(release / installer.REQUIREMENTS)])
        command([python, "-I", "-m", "pip", "--disable-pip-version-check", "check"])
        installer.protect_release(gid)
    # Both checks are pure. Root reads only the explicitly protected master;
    # service code cannot modify it. No --probe or network action is run here.
    disabled = json.loads(command(["/usr/sbin/runuser", "--user", installer.SERVICE_USER,
                                  "--", python, "-I", "scripts/partner_runtime.py", "--check"], cwd=release))
    require(disabled.get("mode") == "disabled" and disabled.get("production_ready") is False,
            "New default runtime is not disabled.")
    check = json.loads(command([python, "-I", "scripts/partner_runtime.py", "--config", str(CONFIG), "--check"], cwd=release))
    require(check.get("configured") is True and check.get("mode") == "staging"
            and check.get("production_ready") is False and not check.get("blockers"),
            "Existing protected configuration is incompatible with the new runtime.")
    staging = release / "deployment-units"
    staging.mkdir(mode=0o750)
    os.chown(staging, 0, gid)
    for name, kind in zip(UNITS, ("http", "worker")):
        installer.write_exclusive(staging / name, render_unit(release, kind), mode=0o640, gid=gid)
    command(["/usr/bin/systemd-analyze", "verify", *(str(staging / name) for name in UNITS)])
    unchanged_config(config_digest)
    unit_state()
    backup.mkdir(mode=0o700)
    for name in UNITS:
        installer.write_exclusive(backup / name, read_regular(UNIT_DIRECTORY / name, mode=0o644), mode=0o600, gid=0)
    changed = False
    try:
        for name in UNITS:
            # A last exact-byte guard before replacing an existing file.
            require(read_regular(UNIT_DIRECTORY / name, mode=0o644) == read_regular(backup / name, mode=0o600),
                    "Existing unit changed during upgrade.")
            changed = True
            atomic_unit(UNIT_DIRECTORY / name, (staging / name).read_bytes())
        command(["/usr/bin/systemctl", "daemon-reload"])
        unchanged_config(config_digest)
        command(["/usr/bin/systemctl", "restart", UNITS[0]])
        unit_state()
        unchanged_config(config_digest)
    except BaseException:
        if changed:
            restore_units(backup)
        raise
    print(json.dumps({"upgraded": True, "release": str(release), "archive_sha256": digest,
                      "rollback_units": str(backup), "http": "active", "worker": "disabled_inactive",
                      "configuration": "unchanged", "payments": "disabled", "database": "not_modified"}, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--confirm-target")
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args(argv)
    if not args.upgrade:
        print(json.dumps({"plan_only": True, "target": TARGET, "old_release": str(OLD_RELEASE),
                          "new_release": "20261022-<explicit-archive-sha256-prefix>",
                          "configuration": "unchanged", "payments": "disabled", "worker": "disabled_inactive",
                          "rollback": "restore_previous_units_and_restart_http", "database": "not_modified"}, sort_keys=True))
        return 0
    try:
        require(args.archive is not None, "Explicit archive required.")
        upgrade(args.archive.absolute(), args.sha256, args.confirm_target)
        return 0
    except KeyboardInterrupt:
        print("Upgrade interrupted; previous units restored if changed. New release retained.", file=sys.stderr)
        return 130
    except Exception:
        print("Upgrade stopped. Raw output and private values suppressed; inspect retained release/backup before retry.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
