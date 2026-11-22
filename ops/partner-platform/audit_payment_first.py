"""Fixed post-release checks; no invoice, bot creation, message or money write.

The existing operator's authenticated settings request refreshes only its normal
manager identity timestamp. Financial/tenant preservation checks are READ ONLY.
No synthetic identity is registered and no secret or customer row is printed.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode
from uuid import UUID

CONFIG = Path("/etc/spn-partner-private/staging.json")
BACKUP = CONFIG.with_name("before-payment-first-20261026.json")
OWNER = 6910097562
FICTIONAL = 9_000_000_000_000_000_001
PROJECT = "crrmaqiakrqpfvhiwtkq"


def require(value):
    if not value:
        raise ValueError("Payment-first verification failed; details suppressed")


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        # Do not let argparse echo an accidentally supplied private argument.
        raise ValueError("Only fixed payment-first verification options are accepted")


def check_entry_snapshot(prior, current):
    columns = ("id", "project_id", "owner_tg_id", "request_key", "purpose", "price_kopecks", "currency")
    require(current is not None and all(str(current[key]) == str(prior[key]) for key in columns))
    # Newly paid or newly bound Managed Bot entries may fill a prior NULL.
    # A pre-existing bot/receipt identity must never be replaced or removed.
    for key in ("bot_id", "provider_payment_id"):
        if prior.get(key) is not None:
            require(str(current[key]) == str(prior[key]))


def proof(token, actor):
    fields = {"auth_date": str(int(time.time())), "query_id": "release-verification",
              "user": json.dumps({"id": actor, "first_name": "Check"}, separators=(",", ":"))}
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, "\n".join(k + "=" + v for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return "tma " + urlencode(fields)


async def audit(release):
    require(sys.platform == "linux" and os.geteuid() == 0
            and release.parent == Path("/opt/spn-franchise-staging/releases")
            and re.fullmatch(r"20261026-[0-9a-f]{12}", release.name)
            and Path(sys.prefix) == release / ".venv")
    sys.path.insert(0, str(release))
    from scripts.partner_runtime import load_config, open_pool
    import aiohttp
    settings, _ = load_config(CONFIG)
    require(settings.enable_public_entry_sales and settings.enable_payment_first_entry
            and not settings.enable_public_entry_trial and not settings.enable_owner_entry_pilot
            and settings.manager_allowed_user_ids is None and settings.operator_ids == (OWNER,)
            and settings.database_project_ref == PROJECT and not settings.enable_provisioning
            and not settings.enable_public_checkout)
    saved = json.loads(BACKUP.read_bytes())
    require(saved.get("project_ref") == PROJECT and len(saved.get("tables", {})) == 32
            and any(row.get("tg_id") == OWNER for row in saved["tables"]["wl_manager_identities"]))
    # This exact real operator must already exist both in the backup and now.
    # Otherwise even a normal settings GET could create a new identity row.
    async with open_pool(settings) as pool:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                require(await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM wl_manager_identities WHERE tg_id=$1)", OWNER
                ) is True)
    # Public access uses a read-only project listing, not settings (which records
    # a central identity). The support guard denies this fictional non-operator.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25), trust_env=False) as session:
        async def get(path, actor=None):
            headers = {"Host": "partners.wayspn.online"}
            if actor is not None:
                headers["authorization"] = proof(settings.manager_bot_token, actor)
            async with session.get("http://127.0.0.1:8966" + path, headers=headers,
                                   allow_redirects=False) as response:
                raw = await response.content.read(131073)
                require(len(raw) <= 131072)
                return response.status, json.loads(raw)
        require((await get("/partners/api/settings"))[0] == 401)
        code, info = await get("/partners/api/settings", OWNER)
        require(code == 200 and info.get("entry_fee_kopecks") == 999000
                and info.get("pay_first_enabled") is True
                and info.get("public_entry_sales") is True
                and info.get("public_entry_trial") is False)
        code, listing = await get("/partners/api/projects", FICTIONAL)
        require(code == 200 and listing.get("items") == [])
        require((await get("/partners/support/api/settings", FICTIONAL))[0] == 403)
        require((await get("/partners/support/api/settings", OWNER))[0] == 200)
    checked = 0
    async with open_pool(settings) as pool:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                for prior in saved["tables"]["wl_entry_orders"]:
                    row = await conn.fetchrow("SELECT * FROM wl_entry_orders WHERE id=$1", UUID(prior["id"]))
                    check_entry_snapshot(prior, row)
                    checked += 1
                # Existing money rows must not be edited/deleted by this release.
                # Entry receipts may legitimately advance status after payment;
                # their immutable financial snapshot was checked separately.
                for table in ("wl_revenue_ledger", "wl_revenue_reversals", "wl_withdrawals"):
                    current = await conn.fetch('SELECT to_jsonb(t)::text AS row FROM public."' + table + '" t')
                    normalized = {json.dumps(json.loads(row["row"]), sort_keys=True) for row in current}
                    require(all(json.dumps(row, sort_keys=True) in normalized for row in saved["tables"][table]))
    return {"entry_price_rub": 9990, "payment_first": True, "public_access_verified": True,
            "anonymous_denied": True, "non_operator_support_denied": True,
            "historical_entry_snapshots_preserved": checked, "financial_rows_preserved": True,
            "operator_identity_refreshed": True, "invoices_created": 0,
            "telegram_messages_sent": 0, "financial_writes": 0}


def main(argv=None):
    try:
        parser = PrivateParser(description=__doc__)
        parser.add_argument("--release", type=Path)
        args = parser.parse_args(argv)
        result = (asyncio.run(asyncio.wait_for(audit(args.release), 150)) if args.release else
                  {"plan_only": True, "network_used": False, "invoices_created": 0})
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print("Payment-first verification stopped; private details suppressed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
