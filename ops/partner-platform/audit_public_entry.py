"""Fixed, read-only post-release checks. Never sends a Telegram message/invoice.

Synthetic central proof is used ONLY for read-only settings/authorization checks
and is never printed or persisted. Database benchmark is enforced READ ONLY.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import statistics
import sys
import time
from urllib.parse import urlencode

RELEASE = Path("/opt/spn-franchise-staging/releases/20261025-bb1171417098")
CONFIG = Path("/etc/spn-partner-private/staging.json")
OWNER = 6910097562
# Outside real Telegram user ID space; no registration/mutation is invoked.
FICTIONAL = 9_000_000_000_000_000_001


def require(value):
    if not value:
        raise ValueError("Read-only public entry verification failed")


def proof(token, actor):
    fields = {"auth_date": str(int(time.time())), "query_id": "read-only-release-check",
              "user": json.dumps({"id": actor, "first_name": "Read-only check"}, separators=(",", ":"))}
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, "\n".join(k + "=" + v for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return "tma " + urlencode(fields)


async def audit():
    require(sys.platform == "linux" and os.geteuid() == 0
            and Path(sys.prefix) == RELEASE / ".venv")
    sys.path.insert(0, str(RELEASE))
    from scripts.partner_runtime import load_config, open_pool
    from services.partner_store import PartnerStore
    import aiohttp
    settings, _ = load_config(CONFIG)
    require(settings.enable_public_entry_trial and not settings.enable_owner_entry_pilot
            and settings.manager_allowed_user_ids is None and settings.operator_ids == (OWNER,)
            and not settings.enable_provisioning and not settings.enable_public_checkout)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session:
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
        code, info = await get("/partners/api/settings", FICTIONAL)
        require(code == 200 and info.get("entry_fee_kopecks") == 1000
                and info.get("public_entry_trial") is True and info.get("is_operator") is False)
        require((await get("/partners/support/api/settings", FICTIONAL))[0] == 403)
        code, info = await get("/partners/support/api/settings", OWNER)
        require(code == 200 and info.get("context") == "platform_support")
        code, listing = await get("/partners/support/api/projects", OWNER)
        require(code == 200 and isinstance(listing.get("items"), list))
    report = {"public_entry_price_rub": 10, "public_settings_verified": True,
              "anonymous_denied": True, "non_operator_support_denied": True,
              "operator_support_verified": True, "telegram_messages_sent": 0,
              "invoices_created": 0, "database_writes": 0}
    async with open_pool(settings) as pool:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                pid = await conn.fetchval("""SELECT p.id FROM wl_projects p
                    JOIN wl_customers c ON c.project_id=p.id AND c.tg_id=$1
                    JOIN wl_campaign_attributions a ON a.project_id=p.id AND a.tg_id=c.tg_id
                    WHERE p.owner_tg_id=$1 AND p.status='active' AND p.bot_id IS NOT NULL
                    ORDER BY p.created_at LIMIT 1""", OWNER)
                if pid is not None:
                    class ReadLease:
                        @asynccontextmanager
                        async def acquire(self):
                            yield conn
                    lease = ReadLease()
                    registry = PartnerStore(lambda: lease)
                    before, after = [], []
                    for _ in range(3):
                        start = time.perf_counter()
                        await registry.get_runtime_project(pid)
                        # ONLY the legacy frozen-registration SELECT, not the
                        # registration service (which also has a write branch).
                        # The surrounding DB transaction independently forbids
                        # writes. No bot/update/customer operation is invoked.
                        prior = await conn.fetchrow("""SELECT c.* FROM wl_customers c
                            JOIN wl_campaign_attributions a
                              ON a.project_id=c.project_id AND a.tg_id=c.tg_id
                            JOIN wl_projects p ON p.id=c.project_id
                            WHERE c.project_id=$1 AND c.tg_id=$2""", pid, OWNER)
                        require(prior is not None)
                        before.append((time.perf_counter() - start) * 1000)
                        start = time.perf_counter()
                        context = await registry.get_bot_update_context(pid, OWNER)
                        require(context["_bot_update_context"].registered
                                and context["_bot_update_context"].attribution_frozen)
                        after.append((time.perf_counter() - start) * 1000)
                    report["navigation_database_benchmark"] = {
                        "old_path_median_ms": round(statistics.median(before), 2),
                        "new_path_median_ms": round(statistics.median(after), 2),
                        "speedup": round(statistics.median(before) / statistics.median(after), 2),
                        "samples": 3, "read_only_transaction": True,
                        "scope": "equivalent_registered_navigation_selects_only_not_telegram_end_to_end",
                    }
                else:
                    report["navigation_database_benchmark"] = {"available": False,
                        "reason": "no_existing_registered_owner_no_test_rows_created"}
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(asyncio.wait_for(audit(), timeout=120)), sort_keys=True))
    except Exception:
        print("Read-only verification stopped; private error details suppressed.", file=sys.stderr)
        raise SystemExit(2)
