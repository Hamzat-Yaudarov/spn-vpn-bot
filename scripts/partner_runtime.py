"""Separate staging entry point. Default --check never connects or starts SPN.

No dotenv/environment credential discovery, automatic migrations or production
mode. The operator must prepare an isolated PostgreSQL database separately.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import signal
import ssl
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.partner_runtime import (
    PartnerRuntimeSettings,
    RuntimeConfigurationError,
    build_runtime,
)
from services.partner_runtime_catalog import StagingAddonCatalog


MAX_CONFIG_BYTES = 65_536


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def load_config(path):
    """Read only the explicitly chosen owner-only JSON, never .env or symlinks."""
    fd = None
    try:
        path = Path(path)
        if path.name == ".env" or path.name.startswith(".env."):
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.geteuid()
            or info.st_size > MAX_CONFIG_BYTES
        ):
            raise ValueError()
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            not isinstance(value, dict)
            or set(value) - {"runtime", "staging_addon_catalog"}
            or "runtime" not in value
        ):
            raise ValueError()
        settings = PartnerRuntimeSettings.from_mapping(value["runtime"])
        snapshot = value.get("staging_addon_catalog")
        if settings.addon_catalog_reviewed and snapshot is None:
            raise ValueError()
        if snapshot is not None and not settings.addon_catalog_reviewed:
            raise ValueError()
        return settings, StagingAddonCatalog(snapshot)
    except Exception:
        raise RuntimeConfigurationError(
            "Нужен явный JSON стенда, доступный только его владельцу (0600)."
        ) from None
    finally:
        if fd is not None:
            os.close(fd)


@asynccontextmanager
async def open_pool(settings):
    """Only explicit --probe/--serve/--worker reaches this network boundary."""
    import asyncpg

    pool = None
    try:
        # Verify the server identity, not only encryption. An optional explicit
        # CA path comes from the protected partner config, never main .env.
        tls = (
            ssl.create_default_context(cafile=settings.database_ca_file or None)
            if settings.database_tls
            else False
        )
        pool = await asyncio.wait_for(
            asyncpg.create_pool(
                dsn=settings.database_dsn,
                ssl=tls,
                statement_cache_size=(
                    0 if settings.database_connection_mode == "session" else 100
                ),
                min_size=1,
                max_size=10,
                command_timeout=15,
                timeout=10,
                server_settings={
                    "application_name": "spn-partner-staging",
                    "statement_timeout": "15000",
                    "lock_timeout": "5000",
                    "idle_in_transaction_session_timeout": "30000",
                    "search_path": "public",
                },
            ),
            timeout=15,
        )
        yield pool
    finally:
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), timeout=10)
            except Exception:
                pool.terminate()


def extension_builder(settings):
    if any(
        (
            settings.management_enabled,
            settings.finance_enabled,
            settings.branding_enabled,
            settings.enable_public_checkout,
        )
    ):
        from partner_platform_web import build_platform_extensions

        return build_platform_extensions
    return None


async def operate(args, settings, catalogue):
    async with open_pool(settings) as pool:
        runtime = build_runtime(
            settings,
            lambda: pool,
            addon_loader=catalogue.quote,
            catalog_loader=catalogue.summary,
            extra_router_builder=extension_builder(settings),
        )
        health = await runtime.probe()
        if args.probe:
            print(json.dumps(health, ensure_ascii=False), flush=True)
            return 0 if health["database_ready"] else 2
        if not health["database_ready"]:
            print(
                json.dumps(
                    {"error": "explicit_staging_migrations_or_database_required"}
                ),
                flush=True,
            )
            return 2
        if args.bootstrap_manager:
            result = await runtime.manager_bootstrap.install(
                replace_existing=args.replace_manager_webhook
            )
            print(json.dumps(result), flush=True)
            return 0 if result["state"] == "installed" else 2
        if args.serve:
            import uvicorn

            # No access logs: URLs contain bearer subscription links. Uvicorn
            # cannot trust arbitrary forwarded headers and never runs in debug.
            server = uvicorn.Server(
                uvicorn.Config(
                    runtime.app(),
                    host="127.0.0.1",
                    port=args.port,
                    access_log=False,
                    log_level="critical",
                    proxy_headers=False,
                    server_header=False,
                    timeout_graceful_shutdown=30,
                )
            )
            await server.serve()
            return 0
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await runtime.worker().run(
                stop, report=lambda value: print(json.dumps(value), flush=True)
            )
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", help="Explicit 0600 staging JSON; never discovered automatically"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--check", action="store_true", help="Pure local configuration check (default)"
    )
    actions.add_argument(
        "--probe", action="store_true", help="Explicit read-only staging database check"
    )
    actions.add_argument(
        "--serve",
        action="store_true",
        help="Explicit isolated staging HTTP on loopback",
    )
    actions.add_argument(
        "--worker",
        action="store_true",
        help="Explicit isolated staging recovery worker",
    )
    parser.add_argument("--port", type=int, default=8966)
    actions.add_argument(
        "--bootstrap-manager",
        action="store_true",
        help="Explicit operator-only central manager webhook installation",
    )
    parser.add_argument(
        "--replace-manager-webhook",
        action="store_true",
        help="Explicitly replace an existing foreign central manager webhook",
    )
    args = parser.parse_args(argv)
    if args.replace_manager_webhook and not args.bootstrap_manager:
        parser.error("Webhook replacement is only valid with --bootstrap-manager")
    if not 1024 <= args.port <= 65535:
        parser.error("Choose an unprivileged loopback port")
    try:
        settings, catalogue = (
            load_config(args.config)
            if args.config
            else (PartnerRuntimeSettings(), StagingAddonCatalog())
        )
        report = settings.preflight()
        if not (args.probe or args.serve or args.worker or args.bootstrap_manager):
            print(json.dumps(report, ensure_ascii=False), flush=True)
            return 0 if settings.mode == "disabled" or report["configured"] else 2
        if not args.config or not report["configured"] or settings.mode != "staging":
            print(
                json.dumps({"error": "explicit_staging_configuration_required"}),
                flush=True,
            )
            return 2
        if (
            args.serve
            and not settings.enable_http
            or args.worker
            and not settings.enable_worker
            or args.bootstrap_manager
            and not settings.enable_manager_webhook
        ):
            print(
                json.dumps({"error": "selected_staging_capability_disabled"}),
                flush=True,
            )
            return 2
        return asyncio.run(operate(args, settings, catalogue))
    except KeyboardInterrupt:
        return 130
    except Exception:
        # No exception text/traceback: DSNs, crypto input and API URLs are secret.
        print(
            json.dumps({"error": "staging_runtime_configuration_or_dependency_error"}),
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
