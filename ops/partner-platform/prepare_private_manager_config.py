"""Prepare only the owner-private manager staging config; default is plan-only.

No network, SQL, service start, webhook installation, credential/key generation
or runtime-source changes. --apply preserves the owner's existing secret values
and exclusively backs up the original bytes before an atomic private replace.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True
ROOT_UID = 0
OWNER_ID = 6910097562
PROJECT_REF = "crrmaqiakrqpfvhiwtkq"
RELEASE = Path("/opt/spn-franchise-staging/releases/20261021-5fab5be5cb97")
CONFIG_PATH = Path("/etc/spn-partner-private/staging.json")
BACKUP_PATH = CONFIG_PATH.with_name("staging.before-manager-enable.json")
CA_PATH = Path("/etc/spn-partner-ca/supabase.crt")
CA_SHA256 = "700723581420dd1ac98fd7e9ac529f0ef210eadcaf87fc868a3ad7d114c2f3b7"
ORIGIN = "https://partners.wayspn.online"
MAX_CONFIG = 65_536
OFF_FLAGS = (
    "enable_customer_webhooks", "enable_billing", "invoice_intents_enabled",
    "panel_identity_enabled", "enable_provisioning", "enable_launches",
    "enable_notifications", "enable_marketing_delivery", "enable_support_delivery",
    "enable_finance_notifications", "enable_public_gateway", "enable_public_checkout",
    "enable_worker", "management_enabled", "finance_enabled", "branding_enabled",
    "addon_catalog_reviewed", "staging_no_addons_confirmed",
)


class PreparationError(ValueError):
    """Fixed messages only; no configuration values or chained exceptions."""


def require(value):
    if not value:
        raise PreparationError("Private manager configuration prerequisites were not met.")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def make_transition(config, SettingsClass):
    """Validate the disabled wizard result and preserve all existing secrets."""
    try:
        require(isinstance(config, dict) and set(config) == {"runtime", "staging_addon_catalog"})
        require(config["staging_addon_catalog"] is None and isinstance(config["runtime"], dict))
        current = SettingsClass.from_mapping(config["runtime"])
        require(current.mode == "disabled" and current.database_ca_file == "")
        require(current.manager_allowed_user_ids == (OWNER_ID,))
        require(current.database_project_ref == PROJECT_REF)
        require(current.database_connection_mode == "session")
        require(current.base_domain == "sub.wayspn.online")
        require(current.manager_bot_username == "spn_franchise_bot")
        require(not current.operator_ids and current.admin_recipient_id is None)
        require(current.manager_origin == "" and current.webhook_origin == "")
        require(not current.enable_http and not current.enable_manager_webhook)
        require(not current.allow_staging_network and not current.allow_staging_writes)
        require(not current.staging_isolation_confirmed)
        require(all(getattr(current, flag) is False for flag in OFF_FLAGS))
        require(current.yookassa_test_mode is True)
        require(all(not getattr(current, name) for name in (
            "yookassa_shop_id", "yookassa_secret_key", "remnawave_api_url",
            "remnawave_api_token", "remnawave_squad_ids", "subscription_source_origin",
        )))
        require(bool(current.manager_bot_token and current.encryption_keys
                     and current.webhook_key_hex and current.subscription_key_hex))
        dsn = urlsplit(current.database_dsn)
        require(dsn.scheme == "postgresql" and dsn.hostname == "aws-0-ap-northeast-1.pooler.supabase.com")
        require(dsn.port == 5432 and dsn.path == "/postgres" and not dsn.query and not dsn.fragment)
        require(unquote(dsn.username or "") == "spn_partner_runtime." + PROJECT_REF)
        require(bool(unquote(dsn.password or "")))
        prepared = deepcopy(config)
        prepared["runtime"].update({flag: False for flag in OFF_FLAGS})
        prepared["runtime"].update(
            mode="staging", database_ca_file=str(CA_PATH), database_tls=True,
            manager_allowed_user_ids=[OWNER_ID], staging_isolation_confirmed=True,
            database_isolation_confirmed=True, database_api_disabled_confirmed=True,
            manager_origin=ORIGIN, webhook_origin=ORIGIN,
            allow_staging_network=True, allow_staging_writes=True,
            enable_http=True, enable_manager_webhook=True,
        )
        verified = SettingsClass.from_mapping(prepared["runtime"])
        require(verified.preflight()["configured"] is True)
        return prepared
    except Exception:
        raise PreparationError("Only the expected untouched disabled owner configuration is accepted.") from None


def no_symlinks(path):
    require(not any(item.is_symlink() for item in (path, *path.parents)))


def check_private_file(info):
    require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1)
    require(stat.S_IMODE(info.st_mode) == 0o600 and 0 < info.st_size <= MAX_CONFIG)


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_uid, stat.S_IMODE(info.st_mode), info.st_nlink)


def check_ca():
    no_symlinks(CA_PATH)
    parent = CA_PATH.parent.stat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == ROOT_UID)
    require(not parent.st_mode & 0o022 and parent.st_mode & 0o005 == 0o005)
    fd = os.open(CA_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1)
        require(not info.st_mode & 0o022 and info.st_mode & 0o004 and 0 < info.st_size <= 1_048_576)
        data = stream.read(1_048_577)
        require(hashlib.sha256(data).hexdigest() == CA_SHA256)


def write_new(directory_fd, name, data):
    """Exclusive root-private creation; remove only our partial file on error."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            require(os.fstat(stream.fileno()).st_uid == ROOT_UID)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        os.unlink(name, dir_fd=directory_fd)
        raise


def apply_private_config(SettingsClass):
    require(CONFIG_PATH.parent == BACKUP_PATH.parent and CONFIG_PATH.name != BACKUP_PATH.name)
    no_symlinks(CONFIG_PATH)
    no_symlinks(BACKUP_PATH)
    require(not BACKUP_PATH.exists())
    check_ca()
    directory_fd = os.open(CONFIG_PATH.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    source_fd = None
    temporary = None
    try:
        parent = os.fstat(directory_fd)
        require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == ROOT_UID)
        require(stat.S_IMODE(parent.st_mode) == 0o700)
        source_fd = os.open(CONFIG_PATH.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        before = os.fstat(source_fd)
        check_private_file(before)
        raw = os.read(source_fd, MAX_CONFIG + 1)
        require(len(raw) == before.st_size and file_identity(os.fstat(source_fd)) == file_identity(before))
        prepared = make_transition(json.loads(raw, object_pairs_hook=unique_object), SettingsClass)
        data = json.dumps(prepared, ensure_ascii=False, indent=2).encode() + b"\n"
        require(len(data) <= MAX_CONFIG)
        # The backup contains original bytes, including the original key set.
        write_new(directory_fd, BACKUP_PATH.name, raw)
        os.fsync(directory_fd)
        candidate = ".manager-config-" + secrets.token_hex(12) + ".tmp"
        write_new(directory_fd, candidate, data)
        temporary = candidate
        os.fsync(directory_fd)
        current = os.stat(CONFIG_PATH.name, dir_fd=directory_fd, follow_symlinks=False)
        check_private_file(current)
        require(file_identity(current) == file_identity(before))
        os.lseek(source_fd, 0, os.SEEK_SET)
        require(os.read(source_fd, MAX_CONFIG + 1) == raw)
        os.replace(temporary, CONFIG_PATH.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = None
        os.fsync(directory_fd)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        if source_fd is not None:
            os.close(source_fd)
        os.close(directory_fd)


def installed_settings():
    no_symlinks(RELEASE)
    info = RELEASE.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
    sys.path.insert(0, str(RELEASE))
    module = importlib.import_module("services.partner_runtime")
    require(Path(module.__file__).resolve().is_relative_to(RELEASE.resolve()))
    return module.PartnerRuntimeSettings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Back up and atomically prepare only the existing private config")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"plan_only": True, "secret_files_read": False,
                          "configuration": "minimal_owner_manager_staging",
                          "services_started": False, "network_used": False}, sort_keys=True))
        return 0
    try:
        require(os.geteuid() == 0 and sys.platform == "linux")
        apply_private_config(installed_settings())
        print(json.dumps({"ok": True, "configuration": "minimal_owner_manager_staging",
                          "original_private_backup": True, "services_started": False,
                          "webhook_installed": False, "network_used": False}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error": "private_manager_config_preparation_stopped"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
