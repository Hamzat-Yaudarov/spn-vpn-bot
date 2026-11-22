"""Bounded read-only database latency diagnosis; never replay a bot update.

Default is a pure plan. --measure needs the exact private config and an explicit
trusted franchise release. Only connection/pool timings and SELECT 1 timings
are printed; no tenant rows, IDs, tokens, DSN, SQL errors or response bodies.
Run with the selected release's own .venv/bin/python.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import statistics
import stat
import sys
import time


ROOT_UID = 0
CONFIG = Path("/etc/spn-partner-private/staging.json")
RELEASES = Path("/opt/spn-franchise-staging/releases")
PROJECT_REF = "crrmaqiakrqpfvhiwtkq"
OWNER_ID = 6910097562


class DiagnosisError(ValueError):
    pass


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise DiagnosisError("Only the fixed diagnostic options are accepted.")


def require(ok):
    if not ok:
        raise DiagnosisError("Read-only latency diagnostic prerequisites were not met.")


def summary(values):
    return {"min_ms": round(min(values), 3), "median_ms": round(statistics.median(values), 3),
            "max_ms": round(max(values), 3)}


async def measure_selects(settings, open_pool, *, samples=5, clock=time.perf_counter):
    """Explicit SELECT-only application workload on its own temporary pool."""
    require(type(samples) is int and 1 <= samples <= 10)
    acquire, release, plain, parameterized = [], [], [], []
    start = clock()
    async with open_pool(settings) as pool:
        pool_open_ms = (clock() - start) * 1000
        for _ in range(samples):
            acquired = clock()
            async with pool.acquire() as conn:
                acquire.append((clock() - acquired) * 1000)
                before = clock()
                require(await conn.fetchval("SELECT 1") == 1)
                plain.append((clock() - before) * 1000)
                before = clock()
                require(await conn.fetchval("SELECT $1::integer", 1) == 1)
                parameterized.append((clock() - before) * 1000)
                released = clock()
            release.append((clock() - released) * 1000)
    return {"read_only": True, "bot_updates_replayed": 0, "samples": samples,
            "select_calls": samples * 2, "pool_open_ms": round(pool_open_ms, 3),
            "pool_acquire": summary(acquire), "pool_release": summary(release),
            "plain_select": summary(plain), "parameterized_select": summary(parameterized),
            "scope": "database_round_trips_only_not_end_to_end_telegram"}


def trusted_runtime(release, config):
    require(os.geteuid() == ROOT_UID and sys.platform == "linux")
    require(config == str(CONFIG) and isinstance(release, str))
    selected = Path(release)
    require(str(selected) == release and selected.parent == RELEASES and ".." not in selected.parts)
    require(Path(sys.executable).absolute() == selected / ".venv/bin/python")
    for path in (selected, *selected.parents, CONFIG.parent):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == ROOT_UID and not info.st_mode & 0o022)
    require(stat.S_IMODE(CONFIG.parent.stat().st_mode) == 0o700)
    info = CONFIG.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
    for name in ("scripts/partner_runtime.py", "services/partner_runtime.py"):
        path = selected / name
        for ancestor in (path.parent, *path.parent.parents):
            require(not ancestor.is_symlink())
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_UID and not info.st_mode & 0o022)
    sys.path.insert(0, str(selected))
    runtime = importlib.import_module("scripts.partner_runtime")
    require(Path(runtime.__file__).resolve() == selected / "scripts/partner_runtime.py")
    settings, _ = runtime.load_config(CONFIG)
    require(settings.mode == "staging" and settings.preflight()["configured"] is True
            and settings.database_isolation_confirmed and settings.database_api_disabled_confirmed
            and settings.database_project_ref == PROJECT_REF
            and settings.manager_allowed_user_ids == (OWNER_ID,))
    return settings, runtime.open_pool


def main(argv=None):
    try:
        parser = PrivateParser(description=__doc__)
        parser.add_argument("--measure", action="store_true")
        parser.add_argument("--release", help="Explicit reviewed release directory")
        parser.add_argument("--config", help="Exact protected master JSON")
        parser.add_argument("--samples", type=int, default=5)
        args = parser.parse_args(argv)
        require(1 <= args.samples <= 10)
        if not args.measure:
            print(json.dumps({"plan_only": True, "secret_files_read": False, "network_used": False,
                              "bot_updates_replayed": 0, "schema_or_data_changed": False}))
            return 0
        settings, open_pool = trusted_runtime(args.release, args.config)
        report = asyncio.run(asyncio.wait_for(
            measure_selects(settings, open_pool, samples=args.samples), timeout=90
        ))
        print(json.dumps(report, sort_keys=True))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Read-only latency diagnostic canceled; no bot update was replayed.", file=sys.stderr)
        return 130
    except Exception:
        print("Read-only latency diagnostic stopped; private configuration and error details suppressed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
