"""Read-only Telegram getMe check for the one reviewed franchise manager.

Default is a local plan. Explicit --check reads only its root-private config;
no token, request URL or raw exception can reach output. No webhook writes.
Ship beside diagnose_bot_latency.py and use the reviewed release's Python.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys


async def check(release):
    from diagnose_bot_latency import trusted_runtime
    settings, _ = trusted_runtime(release, "/etc/spn-partner-private/staging.json")
    from services.partner_launcher import TelegramLaunchAdapter
    result = await TelegramLaunchAdapter().get_me(settings.manager_bot_token)
    if result.get("username", "").lower() != "spn_franchise_bot":
        raise ValueError()
    print(json.dumps({"manager_verified": True, "can_manage_bots": result.get("can_manage_bots") is True,
                      "webhooks_changed": False, "messages_sent": 0}))


def main():
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--check", action="store_true")
        parser.add_argument("--release")
        args = parser.parse_args()
        if not args.check:
            print(json.dumps({"plan_only": True, "network_used": False, "secret_files_read": False}))
            return 0
        # -I omits the script folder. The companion must itself be protected.
        companion = Path(__file__).with_name("diagnose_bot_latency.py")
        info = companion.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or companion.is_symlink():
            raise ValueError()
        sys.path.insert(0, str(companion.parent))
        asyncio.run(asyncio.wait_for(check(args.release), timeout=30))
        return 0
    except Exception:
        print("Managed capability check stopped; private details suppressed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
