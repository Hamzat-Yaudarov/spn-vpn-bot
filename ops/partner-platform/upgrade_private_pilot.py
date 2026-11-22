"""Fixed-target private-pilot upgrade; default is a pure, no-I/O plan.

Ship beside reviewed install_staging.py. --upgrade changes immutable code and
the two exact units, never the master. --enable-new-features is a SEPARATE
transaction: readonly schema/capability checks, pause worker, same-scope manager
bootstrap, two flags only, HTTP readiness, resume worker. No DDL or invoices.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import types
from urllib.parse import unquote, urlsplit
import urllib.request


HELPER = Path(__file__).with_name("install_staging.py")
HELPER_SHA256 = "74fc7c44d6927c1c01c051223387c1b6a4aaf33a317b3c6ae988de95e00bb4f6"
installer = None
TARGET, OWNER, PROJECT = "92.42.102.103", 6910097562, "crrmaqiakrqpfvhiwtkq"
RELEASES = Path("/opt/spn-franchise-staging/releases")
OLD_RELEASE = RELEASES / "20261022-2186dc35d20e"
PRIVATE = Path("/etc/spn-partner-private")
UNITS = ("spn-partner-http.service", "spn-partner-worker.service")
UNIT_DIRECTORY = Path("/etc/systemd/system")
CONFIG = PRIVATE / "staging.json"
DATA_BACKUP = PRIVATE / "before-managed-bots-20261022.json"
FEATURE_BACKUP = PRIVATE / "staging.before-managed-bots.json"
PENDING = PRIVATE / "staging.managed-bots.pending.json"
FEATURES = ("enable_managed_bots", "enable_entry_launch_notifications")
ON_FLAGS = ("enable_owner_entry_pilot", "enable_billing", "enable_http", "enable_manager_webhook",
            "enable_customer_webhooks", "enable_launches", "enable_worker", "database_tls",
            "database_isolation_confirmed", "database_api_disabled_confirmed",
            "staging_isolation_confirmed", "allow_staging_network", "allow_staging_writes")
OFF_FLAGS = ("yookassa_test_mode", "enable_provisioning", "invoice_intents_enabled", "panel_identity_enabled",
             "enable_public_gateway", "enable_public_checkout", "enable_notifications",
             "enable_marketing_delivery", "enable_support_delivery", "enable_finance_notifications",
             "addon_catalog_reviewed", "staging_no_addons_confirmed")


class UpgradeError(ValueError):
    pass


def require(value, message="Private-pilot prerequisite failed; details suppressed."):
    if not value:
        raise UpgradeError(message)


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise UpgradeError("Only fixed private-pilot options are accepted.")


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value))
    return value


def release_for(value):
    return RELEASES / ("20261022-" + digest(value)[:12])


def read_regular(path, *, mode=0o600, maximum=65_536):
    require(not any(item.is_symlink() for item in (path, *path.parents)))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o022 and 0 < info.st_size <= maximum
                and (mode is None or stat.S_IMODE(info.st_mode) == mode))
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns,
                                 item.st_mode, item.st_nlink, item.st_uid)
        require(len(raw) == info.st_size and identity(info) == identity(after))
        return raw
    finally:
        os.close(fd)


def document(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def load_installer():
    global installer
    raw = read_regular(HELPER, mode=None, maximum=100_000)
    require(hashlib.sha256(raw).hexdigest() == HELPER_SHA256)
    # Execute ONLY the exact authenticated bytes, not a reopened raceable path.
    module = types.ModuleType("_private_pilot_installer")
    module.__file__ = str(HELPER)
    exec(compile(raw, str(HELPER), "exec"), module.__dict__)
    installer = module


def validate_config(raw, *, features=False):
    value = document(raw)
    require(isinstance(value, dict) and set(value) == {"runtime", "staging_addon_catalog"}
            and value["staging_addon_catalog"] is None)
    settings = value["runtime"]
    require(isinstance(settings, dict) and settings.get("mode") == "staging"
            and settings.get("manager_allowed_user_ids") == settings.get("operator_ids") == [OWNER]
            and settings.get("admin_recipient_id") == OWNER
            and all(settings.get(key) is True for key in ON_FLAGS)
            and all(settings.get(key, False) is False for key in OFF_FLAGS)
            and all(settings.get(key, False) is features for key in FEATURES))
    require(settings.get("database_project_ref") == PROJECT
            and settings.get("database_connection_mode") == "session"
            and settings.get("database_ca_file") == "/etc/spn-partner-ca/supabase.crt"
            and settings.get("base_domain") == "sub.wayspn.online"
            and settings.get("manager_bot_username") == "spn_franchise_bot"
            and settings.get("manager_origin") == settings.get("webhook_origin") == "https://partners.wayspn.online"
            and all(settings.get(key) for key in ("database_dsn", "manager_bot_token", "encryption_keys",
                                                "webhook_key_hex", "subscription_key_hex",
                                                "yookassa_shop_id", "yookassa_secret_key"))
            and not any(settings.get(key) for key in ("remnawave_api_url", "remnawave_api_token",
                                                    "remnawave_squad_ids", "subscription_source_origin")))
    dsn = urlsplit(settings["database_dsn"])
    require(dsn.scheme == "postgresql" and dsn.hostname == "aws-0-ap-northeast-1.pooler.supabase.com"
            and dsn.port == 5432 and dsn.path == "/postgres" and not dsn.query and not dsn.fragment
            and unquote(dsn.username or "") == "spn_partner_runtime." + PROJECT and bool(dsn.password))
    return value


def candidate(raw):
    value = deepcopy(validate_config(raw))
    value["runtime"].update({name: True for name in FEATURES})
    result = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    validate_config(result, features=True)
    return result


def command(args, *, cwd=None, timeout=400, allowed_codes=(0,)):
    result = subprocess.run(args, cwd=cwd, env=installer.SAFE_ENV, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    require(result.returncode in allowed_codes and len(result.stdout) <= 131_072,
            "Required private-pilot command failed; output suppressed.")
    return result.stdout.decode("utf-8").strip()


@contextmanager
def selected_release(release):
    previous = installer.RELEASE
    installer.RELEASE = release
    try: yield
    finally: installer.RELEASE = previous


def render_unit(release, kind):
    with selected_release(release):
        return installer.render_unit(kind).encode()


def service_state(release, *, worker_active=True):
    for name, kind in zip(UNITS, ("http", "worker")):
        require(read_regular(UNIT_DIRECTORY / name, mode=0o644) == render_unit(release, kind))
        require(not command(["/usr/bin/systemctl", "show", "--property=DropInPaths", "--value", name]))
        require(command(["/usr/bin/systemctl", "show", "--property=FragmentPath", "--value", name]) == str(UNIT_DIRECTORY / name))
        require(command(["/usr/bin/systemctl", "is-enabled", name]) == "enabled")
        expected = "active" if kind == "http" or worker_active else "inactive"
        require(command(["/usr/bin/systemctl", "is-active", name], allowed_codes=(0, 3, 4)) == expected)


def data_backup(expected):
    raw = read_regular(DATA_BACKUP, maximum=20_000_000)
    require(hashlib.sha256(raw).hexdigest() == digest(expected))
    saved = document(raw)
    require(saved.get("format") == 1 and saved.get("project_ref") == PROJECT
            and isinstance(saved.get("tables"), dict) and len(saved["tables"]) == 31
            and all(isinstance(name, str) and re.fullmatch(r"wl_[a-z_]+", name)
                    and isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                    for name, rows in saved["tables"].items()))


def preflight(target, release, backup_sha):
    require(target == TARGET and sys.platform == "linux" and os.geteuid() == 0 and sys.version_info >= (3, 11))
    load_installer()
    addresses = document(command(["/usr/sbin/ip", "-j", "-4", "address", "show", "scope", "global"]))
    require(any(a.get("local") == TARGET for link in addresses for a in link.get("addr_info", [])))
    account, group = installer.pwd.getpwnam(installer.SERVICE_USER), installer.grp.getgrnam(installer.SERVICE_USER)
    require(account.pw_uid != 0 and account.pw_gid == group.gr_gid
            and account.pw_shell in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"})
    for path in (Path("/opt"), installer.RELEASES.parent, installer.RELEASES, UNIT_DIRECTORY, release):
        installer.directory(path, uid=0)
    installer.directory(PRIVATE, uid=0, exact_mode=0o700)
    installer.directory(installer.STATE, uid=account.pw_uid, gid=group.gr_gid, exact_mode=0o700)
    for name in UNITS:
        for prefix in installer.UNIT_SEARCH:
            for suffix in (name, name + ".d", "spn-partner-.service.d", "spn-.service.d", "service.d"):
                path = prefix / suffix
                if path == UNIT_DIRECTORY / name: continue
                require(not path.exists() and not path.is_symlink())
    data_backup(backup_sha)
    raw = read_regular(CONFIG)
    validate_config(raw)
    service_state(release)
    return group.gr_gid, raw


def unchanged(raw):
    require(read_regular(CONFIG) == raw, "Master changed concurrently; stop for operator review.")


def unused(path):
    require(not path.exists() and not path.is_symlink(), "Exclusive transaction path already exists; inspect before retry.")


def write_new(path, raw, mode=0o600):
    installer.write_exclusive(path, raw, mode=mode, gid=0)


def atomic(path, raw, mode=0o600):
    temporary = path.with_name("." + path.name + ".private-pilot-new")
    write_new(temporary, raw, mode)
    try:
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temporary.exists(): temporary.unlink()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise UpgradeError("Redirect refused.")


def http_ready():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + 40
    request = urllib.request.Request("http://127.0.0.1:8966/partners", headers={"Host": "partners.wayspn.online"})
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=2) as response:
                if response.status == 200: return
        except Exception: pass
        time.sleep(0.5)
    raise UpgradeError("Private HTTP readiness failed.")


def runtime(release, path, action):
    report = document(command([str(release / ".venv/bin/python"), "-I", "scripts/partner_runtime.py",
                               "--config", str(path), action], cwd=release, timeout=90))
    if action == "--bootstrap-manager":
        require(report.get("state") == "installed", "Same-scope manager bootstrap failed.")
    else:
        require(report.get("configured") is True and report.get("production_ready") is False and not report.get("blockers"))
        if action == "--probe":
            require(report.get("database_ready") is True and report.get("database_checked") is True)


def manager_capability(release):
    # Only readonly getMe/getWebhookInfo; no token or identity is returned.
    code = """import asyncio,json,sys
sys.path.insert(0, sys.argv[1])
from scripts.partner_runtime import load_config
from services.partner_manager import TelegramManagerAdapter
async def check():
 s,_=load_config(sys.argv[2]); t=TelegramManagerAdapter()
 me=await t.get_me(s.manager_bot_token); wh=await t.get_webhook_info(s.manager_bot_token)
 ok=(me.get('id')==int(s.manager_bot_token.split(':',1)[0]) and me.get('is_bot') is True
  and me.get('username','').lower()==s.manager_bot_username.lower() and me.get('can_manage_bots') is True
  and wh.get('url')==s.webhook_origin+'/partner-manager/webhook' and wh.get('allowed_updates')==['message'])
 print(json.dumps({'ready':ok}))
asyncio.run(check())
"""
    report = document(command([str(release / ".venv/bin/python"), "-I", "-c", code,
                               str(release), str(CONFIG)], cwd=release, timeout=45))
    require(report.get("ready") is True, "Manager mode or existing webhook requires review.")


def upgrade(archive, sha, target, backup_sha):
    release, backup = release_for(sha), PRIVATE / ("upgrade-private-before-" + digest(sha)[:12])
    gid, raw = preflight(target, OLD_RELEASE, backup_sha)
    unused(release); unused(backup)
    files = installer.validate_archive(read_regular(archive, mode=None, maximum=installer.MAX_ARCHIVE), expected_sha256=sha)
    os.umask(0o077)
    with selected_release(release):
        installer.extract_release(files, gid)
        command([sys.executable, "-I", "-m", "venv", "--copies", str(release / ".venv")])
        python = str(release / ".venv/bin/python")
        command([python, "-I", "-m", "pip", "--disable-pip-version-check", "install", "--index-url",
                 "https://pypi.org/simple", "--only-binary=:all:", "--no-cache-dir", "--no-compile",
                 "--require-virtualenv", "-r", str(release / installer.REQUIREMENTS)])
        command([python, "-I", "-m", "pip", "--disable-pip-version-check", "check"])
        installer.protect_release(gid)
    runtime(release, CONFIG, "--check")
    staging = release / "deployment-units"
    staging.mkdir(mode=0o750); os.chown(staging, 0, gid)
    for name, kind in zip(UNITS, ("http", "worker")):
        installer.write_exclusive(staging / name, render_unit(release, kind), mode=0o640, gid=gid)
    command(["/usr/bin/systemd-analyze", "verify", *(str(staging / name) for name in UNITS)])
    unchanged(raw); service_state(OLD_RELEASE)
    backup.mkdir(mode=0o700)
    write_new(backup / "staging.json", raw)
    for name in UNITS: write_new(backup / name, read_regular(UNIT_DIRECTORY / name, mode=0o644))
    paused = False
    try:
        # Set before stop: a timeout can still have stopped the worker.
        paused = True
        command(["/usr/bin/systemctl", "stop", UNITS[1]])
        unchanged(raw); service_state(OLD_RELEASE, worker_active=False)
        for name in UNITS:
            require(read_regular(UNIT_DIRECTORY / name, mode=0o644) == read_regular(backup / name))
            atomic(UNIT_DIRECTORY / name, read_regular(staging / name, mode=0o640), 0o644)
        command(["/usr/bin/systemctl", "daemon-reload"])
        command(["/usr/bin/systemctl", "restart", UNITS[0]])
        http_ready(); unchanged(raw)
        command(["/usr/bin/systemctl", "start", UNITS[1]])
        service_state(release); unchanged(raw)
    except BaseException:
        if paused:
            command(["/usr/bin/systemctl", "stop", UNITS[1]])
            for name in UNITS: atomic(UNIT_DIRECTORY / name, read_regular(backup / name), 0o644)
            command(["/usr/bin/systemctl", "daemon-reload"])
            unchanged(raw)
            command(["/usr/bin/systemctl", "restart", UNITS[0]])
            http_ready()
            command(["/usr/bin/systemctl", "start", UNITS[1]])
        raise
    return {"upgraded": True, "release": str(release), "master": "unchanged", "services": "active",
            "rollback": str(backup), "new_features": "disabled", "database": "not_modified"}


def enable_features(sha, target, backup_sha):
    release = release_for(sha)
    _, raw = preflight(target, release, backup_sha)
    unused(FEATURE_BACKUP); unused(PENDING)
    prepared = candidate(raw)
    write_new(PENDING, prepared)
    runtime(release, PENDING, "--check")
    runtime(release, PENDING, "--probe")
    manager_capability(release)
    unchanged(raw); require(read_regular(PENDING) == prepared); service_state(release)
    write_new(FEATURE_BACKUP, raw)
    paused, bootstrap_attempted, swapped = False, False, False
    try:
        paused = True
        command(["/usr/bin/systemctl", "stop", UNITS[1]])
        unchanged(raw); service_state(release, worker_active=False)
        require(read_regular(PENDING) == prepared and read_regular(FEATURE_BACKUP) == raw)
        bootstrap_attempted = True
        runtime(release, PENDING, "--bootstrap-manager")
        unchanged(raw)
        swapped = True
        atomic(CONFIG, prepared)
        command(["/usr/bin/systemctl", "restart", UNITS[0]])
        http_ready(); require(read_regular(CONFIG) == prepared)
        command(["/usr/bin/systemctl", "start", UNITS[1]])
        service_state(release); require(read_regular(CONFIG) == prepared)
    except BaseException:
        if paused:
            command(["/usr/bin/systemctl", "stop", UNITS[1]])
            # Refuse overwriting a third-party concurrent edit.
            require(read_regular(CONFIG) in (raw, prepared))
            if swapped: atomic(CONFIG, read_regular(FEATURE_BACKUP))
            unchanged(raw)
            manager_rollback_failed = False
            try:
                if bootstrap_attempted:
                    runtime(release, CONFIG, "--bootstrap-manager")
            except BaseException:
                manager_rollback_failed = True
            finally:
                # Local flags must stop being effective even when Telegram is
                # unavailable or accepted the rollback with a lost response.
                command(["/usr/bin/systemctl", "restart", UNITS[0]])
                http_ready()
            require(not manager_rollback_failed,
                    "Manager rollback requires operator review; worker remains stopped.")
            command(["/usr/bin/systemctl", "start", UNITS[1]])
        raise
    return {"features_enabled": list(FEATURES), "release": str(release), "services": "active",
            "money_configuration": "unchanged", "rollback": str(FEATURE_BACKUP), "database": "not_modified"}


def main(argv=None):
    try:
        parser = PrivateParser(description=__doc__)
        actions = parser.add_mutually_exclusive_group()
        actions.add_argument("--upgrade", action="store_true")
        actions.add_argument("--enable-new-features", action="store_true")
        parser.add_argument("--archive", type=Path)
        parser.add_argument("--sha256")
        parser.add_argument("--backup-sha256")
        parser.add_argument("--confirm-target")
        args = parser.parse_args(argv)
        if not (args.upgrade or args.enable_new_features):
            result = {"plan_only": True, "target": TARGET, "old_release": str(OLD_RELEASE),
                      "phases": ["exclusive_whole_database_backup", "explicit_ddl_separately", "upgrade_unchanged_master",
                                 "readonly_feature_checks_then_explicit_enable"], "invoices": "not_created"}
        elif args.upgrade:
            require(args.archive is not None)
            result = upgrade(args.archive.absolute(), args.sha256, args.confirm_target, args.backup_sha256)
        else:
            require(args.archive is None)
            result = enable_features(args.sha256, args.confirm_target, args.backup_sha256)
        print(json.dumps(result, sort_keys=True))
        return 0
    except BaseException as error:
        if isinstance(error, SystemExit) and error.code == 0: return 0
        print("Private-pilot operation stopped; captured details suppressed. Inspect transaction backups and service state.", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
