"""One fixed, explicit private partner backup; default is a no-I/O plan.

Run with the current installed release's Python. No SQL writes, invoices,
Telegram calls, service changes, migration or secret/row output are performed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import unquote, urlsplit


TARGET = "92.42.102.103"
RELEASE = Path("/opt/spn-franchise-staging/releases/20261022-2186dc35d20e")
PRIVATE = Path("/etc/spn-partner-private")
CONFIG = PRIVATE / "staging.json"
BACKUP = PRIVATE / "before-managed-bots-20261022.json"
OWNER = 6910097562
PROJECT = "crrmaqiakrqpfvhiwtkq"
MAX_BYTES = 20_000_000
EXPECTED_TABLES = 31
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}
MAIN_SIGNATURES = {
    "users": {"tg_id", "remnawave_uuid", "accepted_terms"},
    "payments": {"tg_id", "tariff_code", "invoice_id", "provider"},
    "subscriptions": {"tg_id", "slot_number", "remnawave_uuid"},
}


class BackupError(ValueError):
    pass


def require(value):
    if not value:
        raise BackupError("Fixed private backup prerequisites failed.")


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise BackupError("Only fixed private backup options are accepted.")


def trusted_directory(path, *, private=False):
    for parent in (path, *path.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
    if private:
        require(stat.S_IMODE(path.lstat().st_mode) == 0o700)


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns)


def preflight():
    require(sys.platform == "linux" and os.geteuid() == 0)
    require(Path(sys.prefix) == RELEASE / ".venv")
    trusted_directory(PRIVATE, private=True)
    trusted_directory(RELEASE)
    trusted_directory(RELEASE / "scripts")
    runtime = (RELEASE / "scripts/partner_runtime.py").lstat()
    require(stat.S_ISREG(runtime.st_mode) and runtime.st_uid == 0 and not runtime.st_mode & 0o022)
    source = CONFIG.lstat()
    require(stat.S_ISREG(source.st_mode) and source.st_uid == 0 and source.st_nlink == 1
            and stat.S_IMODE(source.st_mode) == 0o600 and 0 < source.st_size <= 65536)
    try:
        BACKUP.lstat()
    except FileNotFoundError:
        pass
    else:
        raise BackupError("Private backup already exists; overwrite forbidden.")
    result = subprocess.run(["/usr/sbin/ip", "-j", "-4", "address", "show"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=ENV, timeout=10, check=True, shell=False)
    addresses = {row["local"] for interface in json.loads(result.stdout)
                 for row in interface.get("addr_info", []) if row.get("family") == "inet"}
    require(TARGET in addresses)
    return identity(source)


def validate_settings(settings):
    require(settings.mode == "staging" and settings.preflight()["configured"] is True)
    require(settings.manager_allowed_user_ids == settings.operator_ids == (OWNER,)
            and settings.admin_recipient_id == OWNER)
    require(all(getattr(settings, name) is True for name in (
        "enable_owner_entry_pilot", "enable_http", "enable_manager_webhook", "enable_billing",
        "enable_customer_webhooks", "enable_launches", "enable_worker", "database_tls",
        "database_isolation_confirmed", "database_api_disabled_confirmed",
        "staging_isolation_confirmed", "allow_staging_network", "allow_staging_writes",
    )))
    require(all(getattr(settings, name) is False for name in (
        "yookassa_test_mode", "enable_provisioning", "invoice_intents_enabled",
        "enable_public_gateway", "enable_public_checkout", "enable_notifications",
        "enable_marketing_delivery", "enable_support_delivery", "enable_finance_notifications",
    )))
    require(not getattr(settings, "enable_managed_bots", False)
            and not getattr(settings, "enable_entry_launch_notifications", False))
    require(settings.database_project_ref == PROJECT and settings.database_connection_mode == "session"
            and settings.database_ca_file == "/etc/spn-partner-ca/supabase.crt")
    require(settings.manager_bot_username == "spn_franchise_bot"
            and settings.manager_origin == settings.webhook_origin == "https://partners.wayspn.online")
    require(not any((settings.remnawave_api_url, settings.remnawave_api_token,
                     settings.remnawave_squad_ids, settings.subscription_source_origin)))
    dsn = urlsplit(settings.database_dsn)
    require(dsn.scheme == "postgresql" and dsn.hostname == "aws-0-ap-northeast-1.pooler.supabase.com"
            and dsn.port == 5432 and dsn.path == "/postgres" and not dsn.query and not dsn.fragment
            and unquote(dsn.username or "") == "spn_partner_runtime." + PROJECT
            and bool(dsn.password))


async def snapshot(pool, *, maximum=MAX_BYTES):
    """Stream EVERY row in one readonly repeatable-read snapshot, bounded bytes."""
    blob, counts = bytearray(), {}

    def append(value):
        encoded = value.encode("utf-8")
        require(len(blob) + len(encoded) <= maximum)
        blob.extend(encoded)

    append('{"format":1,"project_ref":' + json.dumps(PROJECT) + ',"tables":{')
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            signatures = await conn.fetch("""SELECT table_name,column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name=ANY($1::text[])""", list(MAIN_SIGNATURES))
            found = {}
            for row in signatures:
                found.setdefault(row["table_name"], set()).add(row["column_name"])
            require(not any(columns <= found.get(table, set()) for table, columns in MAIN_SIGNATURES.items()))
            tables = await conn.fetch("""SELECT tablename FROM pg_tables WHERE schemaname='public'
                AND tablename LIKE 'wl\\_%' ESCAPE '\\' ORDER BY tablename""")
            names = [row["tablename"] for row in tables]
            require(len(names) == len(set(names)) == EXPECTED_TABLES
                    and all(isinstance(name, str) and re.fullmatch(r"wl_[a-z_]+", name) for name in names))
            for index, name in enumerate(names):
                append(("," if index else "") + json.dumps(name) + ":[")
                counts[name] = 0
                async for record in conn.cursor('SELECT to_jsonb(t) AS row FROM public."' + name + '" AS t', prefetch=100):
                    row = record["row"]
                    if isinstance(row, str):
                        # Validate a bounded JSON object but retain the original
                        # PostgreSQL text: re-encoding parsed floats can round
                        # fractional JSON values even though money is BIGINT.
                        require(len(row.encode("utf-8")) + len(blob) + bool(counts[name]) <= maximum)
                        def invalid_constant(value):
                            raise BackupError("Non-JSON backup record.")
                        require(isinstance(json.loads(row, parse_constant=invalid_constant), dict))
                        serialized = row
                    else:
                        require(isinstance(row, dict))
                        serialized = json.dumps(row, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":"), allow_nan=False)
                    append(("," if counts[name] else "") + serialized)
                    counts[name] += 1
                append("]")
    append("}}\n")
    return bytes(blob), counts


def write_exclusive(blob):
    trusted_directory(PRIVATE, private=True)
    directory = os.open(PRIVATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd, created = None, None
    try:
        parent = os.fstat(directory)
        require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and stat.S_IMODE(parent.st_mode) == 0o700)
        fd = os.open(BACKUP.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        os.fchmod(fd, 0o600)
        created = os.fstat(fd)
        require(stat.S_ISREG(created.st_mode) and created.st_uid == 0 and created.st_nlink == 1
                and stat.S_IMODE(created.st_mode) == 0o600 and len(blob) <= MAX_BYTES)
        remaining = memoryview(blob)
        while remaining:
            written = os.write(fd, remaining)
            require(written > 0)
            remaining = remaining[written:]
        os.fsync(fd)
        os.fsync(directory)
    except BaseException:
        if created is not None:
            current = os.stat(BACKUP.name, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                os.unlink(BACKUP.name, dir_fd=directory)
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


async def backup():
    original = preflight()
    sys.path.insert(0, str(RELEASE))
    from scripts.partner_runtime import load_config, open_pool

    settings, _ = load_config(CONFIG)
    validate_settings(settings)
    async with open_pool(settings) as pool:
        blob, counts = await snapshot(pool)
    require(identity(CONFIG.lstat()) == original)
    write_exclusive(blob)
    return {"backup_saved": True, "sha256": hashlib.sha256(blob).hexdigest(),
            "number_tables": len(counts), "table_counts": counts}


def main(argv=None):
    try:
        parser = PrivateParser(description=__doc__)
        parser.add_argument("--backup-db", action="store_true")
        parser.add_argument("--confirm-target", choices=(TARGET,))
        args = parser.parse_args(argv)
        if not args.backup_db:
            print(json.dumps({"plan_only": True, "target": TARGET, "database_read": False,
                              "file_written": False, "expected_tables": EXPECTED_TABLES}))
            return 0
        require(args.confirm_target == TARGET)
        print(json.dumps(asyncio.run(backup()), sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"backup_saved": False, "error": "private_backup_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
