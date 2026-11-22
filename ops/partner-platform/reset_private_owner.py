"""One approved, fixed-target owner-test reset. Default is a pure no-I/O plan.

Archive every row before disabling ONE tenant webhook and deleting its isolated
test records. Never calls a payment provider, changes schema/configuration,
disables a trigger, truncates, or modifies the main SPN. A second run is refused.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import types
import urllib.request
from uuid import UUID


TARGET, OWNER, PROJECT_REF = "92.42.102.103", 6910097562, "crrmaqiakrqpfvhiwtkq"
RELEASE = Path("/opt/spn-franchise-staging/releases/20261022-8b7e781b30fd")
PRIVATE = Path("/etc/spn-partner-private")
CONFIG = PRIVATE / "staging.json"
BACKUP = PRIVATE / "before-owner-reset-20261022.json"
HELPER = Path(__file__).with_name("backup_private_pilot.py")
HELPER_SHA256 = "cc18c85993b7ba56ff16c93835d4a9c02f25ed25b4a84d73d32cfb2ba3545098"
UNITS = ("spn-partner-http.service", "spn-partner-worker.service")
TABLES = frozenset("""wl_projects wl_project_create_requests wl_project_audit
wl_entry_orders wl_launch_jobs wl_partner_outbox wl_managed_bot_requests
wl_customers wl_subscriptions wl_orders wl_activation_jobs wl_revenue_ledger
wl_customer_outbox wl_device_grants wl_referral_bonus_grants wl_traffic_credits
wl_manager_identities wl_project_members wl_management_requests wl_campaigns
wl_campaign_attributions wl_marketing_preferences wl_marketing_broadcasts
wl_marketing_recipients wl_support_resend_intents wl_withdrawals
wl_revenue_reversals wl_finance_audit wl_finance_outbox wl_brand_assets
wl_brand_asset_audit wl_public_renewal_requests""".split())
EMPTY_REQUIRED = frozenset("""wl_activation_jobs wl_revenue_ledger wl_customer_outbox
wl_device_grants wl_referral_bonus_grants wl_traffic_credits wl_project_members
wl_marketing_broadcasts wl_marketing_recipients wl_support_resend_intents
wl_withdrawals wl_revenue_reversals wl_finance_audit wl_finance_outbox
wl_public_renewal_requests""".split())


class ResetError(ValueError):
    pass


def require(value):
    if not value:
        raise ResetError("Approved private reset prerequisites failed; details suppressed.")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ResetError("Only the fixed approved reset options are accepted.")


def load_helper():
    # Authenticate the exact reviewed bytes, not a raceable second import.
    for item in (HELPER, *HELPER.parents):
        info = item.lstat()
        require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)
    fd = os.open(HELPER, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size < 100_000)
        raw = os.read(fd, 100_001)
        require(len(raw) == info.st_size and hashlib.sha256(raw).hexdigest() == HELPER_SHA256)
    finally:
        os.close(fd)
    module = types.ModuleType("_reviewed_private_backup")
    module.__file__ = str(HELPER)
    exec(compile(raw, str(HELPER), "exec"), module.__dict__)
    module.RELEASE, module.BACKUP, module.EXPECTED_TABLES = RELEASE, BACKUP, len(TABLES)
    return module


def validate_tables(tables):
    require(isinstance(tables, dict) and set(tables) == TABLES)
    require(all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                for rows in tables.values()))
    require(len(tables["wl_projects"]) == len(tables["wl_entry_orders"]) == len(tables["wl_launch_jobs"]) == 1)
    project, order, launch = (tables[name][0] for name in ("wl_projects", "wl_entry_orders", "wl_launch_jobs"))
    pid = str(UUID(project["id"]))
    require(project.get("owner_tg_id") == OWNER and project.get("status") == "active"
            and type(project.get("bot_id")) is int and project["bot_id"] > 0
            and isinstance(project.get("encrypted_token"), str))
    for name, rows in tables.items():
        if name in EMPTY_REQUIRED:
            require(not rows)
        for row in rows:
            if name == "wl_projects":
                continue
            if name == "wl_manager_identities":
                require(row.get("tg_id") == OWNER)
            else:
                require(row.get("project_id") == pid)
            if "owner_tg_id" in row:
                require(row["owner_tg_id"] == OWNER)
            if name in {"wl_customers", "wl_subscriptions", "wl_orders", "wl_campaign_attributions", "wl_marketing_preferences"}:
                require(row.get("tg_id") == OWNER)
    require(order.get("status") == "launched" and order.get("price_kopecks") == 1000
            and order.get("currency") == "RUB" and order.get("paid_at")
            and order.get("launched_at") and order.get("provider_payment_id")
            and order.get("bot_id") == project["bot_id"])
    require(launch.get("status") == "complete" and launch.get("order_id") == order.get("id")
            and launch.get("bot_id") == project["bot_id"] and launch.get("completed_at"))
    for row in tables["wl_orders"]:
        require(row.get("status") in {"created", "canceled"} and row.get("paid_at") is None
                and row.get("provider_payment_id") is None and row.get("confirmation_url") is None
                and row.get("invoice_requested_at") is None)
    for row in tables["wl_subscriptions"]:
        require(all(row.get(key) is None for key in (
            "remote_user_id", "subscription_url", "expires_at", "desired_expires_at"))
            and row.get("last_activation_sequence") == 0)
    for row in tables["wl_partner_outbox"]:
        require(row.get("order_id") == order["id"] and row.get("kind") in {"entry_paid", "entry_launched"}
                and row.get("delivery_state") in {"pending", "delivered", "blocked"})
    for row in tables["wl_managed_bot_requests"]:
        require(row.get("bot_id") in {None, project["bot_id"]})
    return project


def freeze(value):
    if isinstance(value, dict):
        return ("object", tuple((key, freeze(item)) for key, item in sorted(value.items())))
    if isinstance(value, list):
        return ("array", tuple(map(freeze, value)))
    return (type(value).__name__, repr(value))


def same_rows(before, after):
    return set(before) == set(after) and all(Counter(map(freeze, before[name])) == Counter(map(freeze, after[name]))
                                           for name in before)


def deletion_order(edges):
    # All FK endpoints must be inside the exact allowlist. Self-references are
    # removed in a single project-wide DELETE statement (owner-only customers).
    parents = {name: set() for name in TABLES}
    for edge in edges:
        require(edge["child_schema"] == edge["parent_schema"] == "public"
                and edge["child"] in TABLES and edge["parent"] in TABLES)
        if edge["child"] != edge["parent"]:
            parents[edge["child"]].add(edge["parent"])
    result, remaining = [], set(TABLES)
    while remaining:
        leaves = sorted(name for name in remaining
                        if not any(name in parents[other] for other in remaining))
        require(leaves)
        result.extend(leaves)
        remaining.difference_update(leaves)
    return result


def command(args, *, allowed=(0,), timeout=60):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"})
    require(result.returncode in allowed)
    return result.stdout.decode().strip()


def check_units(active=True):
    for unit in UNITS:
        require(command(["systemctl", "is-enabled", unit]) == "enabled")
        require(command(["systemctl", "show", "--property=WorkingDirectory", "--value", unit]) == str(RELEASE))
        require(command(["systemctl", "show", "--property=DropInPaths", "--value", unit]) == "")
        expected = "active" if active else "inactive"
        require(command(["systemctl", "is-active", unit], allowed=(0, 3)) == expected)


def restart_services():
    command(["systemctl", "start", UNITS[0]])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request("http://127.0.0.1:8966/partners", headers={"Host": "partners.wayspn.online"})
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            with opener.open(request, timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        raise ResetError("Private HTTP readiness failed.")
    command(["systemctl", "start", UNITS[1]])
    check_units()


async def delete_webhook(token):
    # Not part of the runtime adapter: this one-off destructive operation is
    # deliberately unavailable to partners and cannot target arbitrary hosts.
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=10, connect=3)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        async with session.post("https://api.telegram.org/bot" + token + "/deleteWebhook",
                                json={"drop_pending_updates": False}, allow_redirects=False) as response:
            require(response.status == 200)
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                require(len(body) <= 65_536)
            value = json.loads(body)
            require(value.get("ok") is True and value.get("result") is True)


async def current_rows(conn):
    tables = {}
    names = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'wl\\_%' ESCAPE '\\'")
    require({row["tablename"] for row in names} == TABLES and len(names) == len(TABLES))
    for name in sorted(TABLES):
        rows = await conn.fetch('SELECT to_jsonb(t) AS row FROM public."' + name + '" AS t')
        tables[name] = [json.loads(row["row"], parse_float=Decimal) if isinstance(row["row"], str)
                        else row["row"] for row in rows]
    return tables


async def commit_outcome(settings, original):
    """A lost COMMIT response is not evidence of rollback. Use a NEW pool."""
    from scripts.partner_runtime import open_pool
    try:
        async with open_pool(settings) as pool:
            async with pool.acquire() as conn:
                async with conn.transaction(isolation="repeatable_read", readonly=True):
                    current = await current_rows(conn)
        if all(not rows for rows in current.values()):
            return "committed"
        if same_rows(original, current):
            return "rolled_back"
    except Exception:
        pass
    return "unknown"


async def clear_records(pool, tables):
    project = validate_tables(tables)
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="serializable"):
            await conn.execute("SET LOCAL lock_timeout='5s'")
            await conn.execute("LOCK TABLE " + ",".join('public."' + name + '"' for name in sorted(TABLES)) + " IN SHARE ROW EXCLUSIVE MODE")
            current = await current_rows(conn)
            require(same_rows(tables, current))
            validate_tables(current)
            edges = await conn.fetch("""SELECT c.relname AS child,p.relname AS parent,
                cn.nspname AS child_schema,pn.nspname AS parent_schema FROM pg_constraint f
                JOIN pg_class c ON c.oid=f.conrelid JOIN pg_namespace cn ON cn.oid=c.relnamespace
                JOIN pg_class p ON p.oid=f.confrelid JOIN pg_namespace pn ON pn.oid=p.relnamespace
                WHERE f.contype='f' AND ((cn.nspname='public' AND c.relname=ANY($1::text[]))
                  OR (pn.nspname='public' AND p.relname=ANY($1::text[])))""", sorted(TABLES))
            for name in deletion_order(edges):
                if not tables[name]:
                    continue  # In particular: never touch append-only money tables.
                if name == "wl_projects":
                    where, args = "id=$1 AND owner_tg_id=$2", (UUID(project["id"]), OWNER)
                elif name == "wl_manager_identities":
                    where, args = "tg_id=$1", (OWNER,)
                else:
                    where, args = "project_id=$1", (UUID(project["id"]),)
                deleted = await conn.execute('DELETE FROM public."' + name + '" WHERE ' + where, *args)
                require(deleted == "DELETE " + str(len(tables[name])))
            counts = await conn.fetch(" UNION ALL ".join("SELECT '" + name + "' AS name,count(*)::int AS count FROM public.\"" + name + '\"' for name in sorted(TABLES)))
            require(len(counts) == len(TABLES) and all(row["count"] == 0 for row in counts))


async def reset():
    helper = load_helper()
    original = helper.preflight()
    sys.path.insert(0, str(RELEASE))
    from scripts.partner_runtime import load_config, open_pool
    from services.partner_security import TokenVault
    from services.partner_launcher import TelegramLaunchAdapter, ALLOWED_UPDATES
    from partner_client_web import webhook_secret
    settings, _ = load_config(CONFIG)
    require(settings.enable_managed_bots is True and settings.enable_entry_launch_notifications is True
            and settings.panel_identity_enabled is False and settings.invoice_intents_enabled is False)
    helper.validate_settings(replace(settings, enable_managed_bots=False, enable_entry_launch_notifications=False))
    check_units()
    stopped = attempted_webhook = committed = uncertain_commit = False
    token = project = None
    adapter = TelegramLaunchAdapter()
    try:
        stopped = True  # Even a partially accepted stop must be recovered.
        command(["systemctl", "stop", UNITS[1], UNITS[0]])
        check_units(active=False)
        async with open_pool(settings) as pool:
            blob, counts = await helper.snapshot(pool)
            payload = json.loads(blob, parse_float=Decimal)
            require(payload["format"] == 1 and payload["project_ref"] == PROJECT_REF)
            tables = payload["tables"]
            project = validate_tables(tables)
            require(helper.identity(CONFIG.lstat()) == original)
            helper.write_exclusive(blob)
            info = BACKUP.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o600)
            fd = os.open(BACKUP, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                require(os.read(fd, helper.MAX_BYTES + 1) == blob)
            finally:
                os.close(fd)
            token = TokenVault(settings.encryption_keys).decrypt(project["id"], project["bot_id"], project["encrypted_token"])
            require(project["bot_id"] != int(settings.manager_bot_token.split(":", 1)[0]))
            identity = await adapter.get_me(token)
            require(identity["id"] == project["bot_id"])
            target = settings.webhook_origin + "/partner-bots/" + project["id"] + "/webhook"
            current = await adapter.get_webhook_info(token)
            require(current["url"] == target and sorted(current.get("allowed_updates", [])) == sorted(ALLOWED_UPDATES))
            attempted_webhook = True
            await delete_webhook(token)
            require((await adapter.get_webhook_info(token))["url"] == "")
            try:
                await clear_records(pool, tables)
            except BaseException:
                outcome = await commit_outcome(settings, tables)
                committed, uncertain_commit = outcome == "committed", outcome == "unknown"
                if not committed:
                    raise
            committed = True
        require(helper.identity(CONFIG.lstat()) == original)
        restart_services()
        return {"reset": True, "archive_sha256": hashlib.sha256(blob).hexdigest(),
                "archived_tables": len(counts), "archived_rows": sum(counts.values()),
                "remaining_test_rows": 0, "old_partner_webhook": "disabled",
                "manager_and_main_spn": "unchanged", "payment_provider_calls": 0,
                "new_entry_price_rub": 10, "services": "active"}
    except BaseException:
        restored = not attempted_webhook
        if attempted_webhook and not committed and not uncertain_commit:
            try:
                target = settings.webhook_origin + "/partner-bots/" + project["id"] + "/webhook"
                secret = webhook_secret(bytes.fromhex(settings.webhook_key_hex), project["id"], project["bot_id"])
                await adapter.set_webhook(token, url=target, secret_token=secret)
                verified = await adapter.get_webhook_info(token)
                restored = (verified["url"] == target
                            and sorted(verified.get("allowed_updates", [])) == sorted(ALLOWED_UPDATES))
            except BaseException:
                restored = False
        if stopped:
            if committed or (restored and not uncertain_commit):
                restart_services()
            else:
                # Keep worker paused on ambiguous Telegram rollback; HTTP uses
                # the untouched DB and config and can still be investigated.
                command(["systemctl", "start", UNITS[0]])
        raise ResetError("Reset did not finish; inspect the protected archive and service status before retry.") from None


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--reset-owner-test", action="store_true")
        parser.add_argument("--confirm-target", choices=(TARGET,))
        parser.add_argument("--confirm-owner", type=int, choices=(OWNER,))
        args = parser.parse_args(argv)
        if not args.reset_owner_test:
            print(json.dumps({"plan_only": True, "target": TARGET, "owner": OWNER,
                              "database_read": False, "changes": False}))
            return 0
        require(args.confirm_target == TARGET and args.confirm_owner == OWNER)
        print(json.dumps(asyncio.run(reset()), sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"reset": False, "error": "private_owner_reset_failed",
                          "retry": "inspect_backup_and_units_first"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
