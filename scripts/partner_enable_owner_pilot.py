"""Prepare, but DO NOT activate, the owner's isolated 10 RUB entry pilot.

Default is a pure plan: no secret-file reads, network, database or service calls.
--prepare requires exact explicit paths and creates only a new root-private
pending candidate. It never overwrites the master or changes encryption keys.
Merchant inputs stay in the owner's hidden terminal or one explicit 0600 file.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import getpass
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import unquote, urlsplit
import warnings

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ROOT_UID = 0
OWNER_ID = 6910097562
PROJECT_REF = "crrmaqiakrqpfvhiwtkq"
CONFIG_PATH = Path("/etc/spn-partner-private/staging.json")
OUTPUT_PATH = Path("/etc/spn-partner-private/staging.owner-entry-pilot.pending.json")
MERCHANT_PATH = Path("/etc/spn-partner-private/yookassa.owner-entry-pilot.json")
MAX_CONFIG = 65_536
ORIGIN = "https://partners.wayspn.online"
OFF_FLAGS = (
    "enable_provisioning", "invoice_intents_enabled", "panel_identity_enabled",
    "enable_public_gateway", "enable_public_checkout", "enable_notifications",
    "enable_marketing_delivery", "enable_support_delivery", "enable_finance_notifications",
    "addon_catalog_reviewed", "staging_no_addons_confirmed",
)
PRESERVED = (
    "database_dsn", "database_ca_file", "manager_bot_token", "encryption_keys",
    "webhook_key_hex", "subscription_key_hex",
)


class PilotPreparationError(ValueError):
    """Fixed, non-secret messages only."""


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # A mistaken credential in argv must not be echoed by argparse.
        raise PilotPreparationError("Only the fixed preparation options are accepted.")


class PrivateCandidate(dict):
    def __repr__(self):
        return "<PrivateOwnerEntryPilotCandidate>"

    __str__ = __repr__


def require(condition):
    if not condition:
        raise PilotPreparationError("Owner entry pilot preparation prerequisites were not met.")


def _source_settings(config):
    from services.partner_runtime import PartnerRuntimeSettings

    require(isinstance(config, dict) and set(config) == {"runtime", "staging_addon_catalog"})
    require(isinstance(config["runtime"], dict) and config["staging_addon_catalog"] is None)
    current = PartnerRuntimeSettings.from_mapping(config["runtime"])
    require(current.mode == "staging" and current.preflight()["configured"] is True)
    require(current.manager_allowed_user_ids == (OWNER_ID,))
    require(current.operator_ids in ((), (OWNER_ID,)) and current.admin_recipient_id in (None, OWNER_ID))
    require(current.enable_http and current.enable_manager_webhook)
    require(current.allow_staging_network and current.allow_staging_writes)
    require(all(getattr(current, name) is False for name in (
        "enable_owner_entry_pilot", "enable_billing", "enable_launches",
        "enable_worker", "enable_customer_webhooks", *OFF_FLAGS,
    )))
    require(current.database_project_ref == PROJECT_REF and current.database_connection_mode == "session")
    require(current.database_tls and current.database_api_disabled_confirmed)
    require(current.database_ca_file == "/etc/spn-partner-ca/supabase.crt")
    require(current.base_domain == "sub.wayspn.online")
    require(current.manager_bot_username == "spn_franchise_bot")
    require(current.manager_origin == current.webhook_origin == ORIGIN)
    require(all(getattr(current, name) for name in PRESERVED))
    require(current.yookassa_test_mode is True)
    require(not current.yookassa_shop_id and not current.yookassa_secret_key)
    require(not any((current.remnawave_api_url, current.remnawave_api_token,
                     current.remnawave_squad_ids, current.subscription_source_origin)))
    dsn = urlsplit(current.database_dsn)
    require(dsn.hostname == "aws-0-ap-northeast-1.pooler.supabase.com"
            and dsn.port == 5432 and dsn.path == "/postgres"
            and unquote(dsn.username or "") == "spn_partner_runtime." + PROJECT_REF
            and bool(dsn.password))
    return current


def make_candidate(config, *, shop_id, secret_key):
    """Pure builder. Preserves existing private values exactly; never enables I/O."""
    try:
        from services.partner_runtime import PartnerRuntimeSettings
        from services.partner_yookassa import PartnerYooKassaSettings

        _source_settings(config)
        PartnerYooKassaSettings(shop_id, secret_key, ORIGIN + "/partners",
                               test_mode=False, owner_entry_pilot=True)
        prepared = PrivateCandidate(deepcopy(config))
        runtime = prepared["runtime"]
        runtime.update({name: False for name in OFF_FLAGS})
        runtime.update(
            enable_owner_entry_pilot=True, enable_billing=True, yookassa_test_mode=False,
            enable_launches=True, enable_customer_webhooks=True, enable_worker=True,
            enable_http=True, enable_manager_webhook=True,
            operator_ids=[OWNER_ID], admin_recipient_id=OWNER_ID,
            management_enabled=True, finance_enabled=True, branding_enabled=True,
            yookassa_shop_id=shop_id, yookassa_secret_key=secret_key,
        )
        verified = PartnerRuntimeSettings.from_mapping(runtime)
        require(verified.preflight()["configured"] is True)
        require(all(runtime[name] == config["runtime"][name] for name in PRESERVED))
        return prepared
    except Exception:
        raise PilotPreparationError("The expected untouched private manager and explicit merchant are required.") from None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_uid, stat.S_IMODE(info.st_mode), info.st_nlink)


def _no_symlinks(path):
    require(not any(item.is_symlink() for item in (path, *path.parents)))


def _read_private(directory, name):
    from scripts.partner_runtime import _unique_object

    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and info.st_nlink == 1)
        require(stat.S_IMODE(info.st_mode) == 0o600 and 0 < info.st_size <= MAX_CONFIG)
        raw = os.read(fd, MAX_CONFIG + 1)
        require(len(raw) == info.st_size and _identity(os.fstat(fd)) == _identity(info))
        parsed = json.loads(raw, object_pairs_hook=_unique_object)
        return raw, parsed, _identity(info)
    finally:
        os.close(fd)


def _unused_output(directory):
    try:
        os.stat(OUTPUT_PATH.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise PilotPreparationError("Pending configuration already exists; overwrite is forbidden.")


def _prompt_merchant():
    require(sys.stdin.isatty())
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        shop_id = getpass.getpass("ЮKassa shopId (скрытый ввод): ")
        secret_key = getpass.getpass("Секретный ключ ЮKassa (скрытый ввод): ")
    return shop_id, secret_key


def prepare_pending(*, merchant_file=False, merchant_reader=None):
    """Create the pending file only; master bytes must remain unchanged."""
    require(os.geteuid() == ROOT_UID and sys.platform == "linux")
    require(CONFIG_PATH.parent == OUTPUT_PATH.parent == MERCHANT_PATH.parent)
    require(len({CONFIG_PATH.name, OUTPUT_PATH.name, MERCHANT_PATH.name}) == 3)
    for path in (CONFIG_PATH, OUTPUT_PATH, MERCHANT_PATH):
        _no_symlinks(path)
    directory = os.open(CONFIG_PATH.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    created = False
    try:
        parent = os.fstat(directory)
        require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == ROOT_UID
                and stat.S_IMODE(parent.st_mode) == 0o700)
        _unused_output(directory)
        raw, config, original_identity = _read_private(directory, CONFIG_PATH.name)
        _source_settings(config)
        if merchant_file:
            _, merchant, _ = _read_private(directory, MERCHANT_PATH.name)
            require(isinstance(merchant, dict) and set(merchant) == {"shop_id", "secret_key"})
            shop_id, secret_key = merchant["shop_id"], merchant["secret_key"]
        else:
            shop_id, secret_key = (merchant_reader or _prompt_merchant)()
        candidate = make_candidate(config, shop_id=shop_id, secret_key=secret_key)
        data = json.dumps(candidate, ensure_ascii=False, indent=2).encode() + b"\n"
        require(0 < len(data) <= MAX_CONFIG)
        current_raw, _, current_identity = _read_private(directory, CONFIG_PATH.name)
        require(current_raw == raw and current_identity == original_identity)
        fd = os.open(OUTPUT_PATH.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        created = True
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        require(info.st_uid == ROOT_UID and info.st_nlink == 1)
        remaining = data
        while remaining:
            written = os.write(fd, remaining)
            require(written > 0)
            remaining = remaining[written:]
        os.fsync(fd)
        os.fsync(directory)
        current_raw, _, current_identity = _read_private(directory, CONFIG_PATH.name)
        require(current_raw == raw and current_identity == original_identity)
        return OUTPUT_PATH
    except BaseException:
        if created:
            os.unlink(OUTPUT_PATH.name, dir_fd=directory)
            os.fsync(directory)
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Create pending config only; does not activate it")
    parser.add_argument("--config", help="Explicit fixed private master path")
    parser.add_argument("--output", help="Explicit fixed NEW pending path")
    parser.add_argument("--merchant-file", help="Explicit fixed optional 0600 merchant JSON instead of hidden input")
    try:
        args = parser.parse_args(argv)
        if args.config is not None:
            require(args.config == str(CONFIG_PATH))
        if args.output is not None:
            require(args.output == str(OUTPUT_PATH))
        if args.merchant_file is not None:
            require(args.merchant_file == str(MERCHANT_PATH))
        if not args.prepare:
            print(json.dumps({"plan_only": True, "secret_files_read": False, "pending_only": True,
                              "master_overwritten": False, "network_used": False, "services_started": False}))
            return 0
        require(args.config == str(CONFIG_PATH) and args.output == str(OUTPUT_PATH))
        prepare_pending(merchant_file=args.merchant_file is not None)
        print(json.dumps({"ok": True, "pending_only": True, "master_overwritten": False,
                          "network_used": False, "services_started": False, "payments_enabled": False}))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Preparation canceled; the master configuration was not changed.", file=sys.stderr)
        return 130
    except Exception:
        print("Private pilot preparation stopped; no master configuration or service was changed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
