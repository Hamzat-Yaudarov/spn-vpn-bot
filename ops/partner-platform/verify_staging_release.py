"""Offline composition smoke for the fixed installed release; never starts it.

Run with that release's Python as spn-partner. All credentials are artificial
in-memory fixtures. No configuration, database, sockets, server or worker loop
is opened. This is dependency/composition evidence, not live readiness proof.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
from contextlib import ExitStack
import hashlib
import hmac
import importlib
import importlib.abc
import importlib.metadata
import inspect
import io
import json
import os
from pathlib import Path
import pwd
import socket
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

sys.dont_write_bytecode = True
RELEASE = Path("/opt/spn-franchise-staging/releases/20261021-5fab5be5cb97")
OWNER = 42
TOKEN = "123456789:" + "t" * 35
MODULES = (
    "partner_brand_web", "partner_client_web", "partner_finance_web",
    "partner_management_web", "partner_manager_web", "partner_platform_web",
    "partner_public_checkout_web", "partner_subscription_web", "partner_web",
    "services.partner_bot", "services.partner_brand_assets", "services.partner_catalog",
    "services.partner_checkout", "services.partner_commerce_store",
    "services.partner_enrollment_store", "services.partner_entitlements",
    "services.partner_entry_checkout", "services.partner_finance",
    "services.partner_finance_notifications", "services.partner_launcher",
    "services.partner_management", "services.partner_manager",
    "services.partner_manager_access", "services.partner_notifications",
    "services.partner_panel_identity", "services.partner_public_checkout",
    "services.partner_recovery", "services.partner_remnawave", "services.partner_runtime",
    "services.partner_runtime_catalog", "services.partner_security", "services.partner_store",
    "services.partner_subscription_store", "services.partner_subscriptions",
    "services.partner_traffic", "services.partner_traffic_store", "services.partner_web_auth",
    "services.partner_yookassa", "services.telegram_auth", "services",
    "scripts.partner_runtime", "scripts.partner_setup",
)


class SmokeFailure(Exception):
    pass


def require(condition):
    if not condition:
        raise SmokeFailure()


class NoMainConfig(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in {"config", "database", "dotenv"}:
            raise SmokeFailure()
        return None


def authorization(user_id):
    fields = {"auth_date": str(int(time.time())), "query_id": "offline-smoke",
              "user": json.dumps({"id": user_id, "first_name": "Synthetic"}, separators=(",", ":"))}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return "tma " + urlencode(fields)


async def request(app, path, *, method="GET", headers=(), body=b""):
    messages = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "https",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"manager.example.test"), *headers],
        "client": ("127.0.0.1", 1), "server": ("manager.example.test", 443),
    }, receive, send)
    starts = [message for message in messages if message["type"] == "http.response.start"]
    require(len(starts) == 1)
    return starts[0]["status"], b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )


def effective_routes(routes, *, depth=0):
    """Inspect both flat and 0.156.8 lazy routing without dropping any entries.

    FastAPI's prefix-aware effective_candidates() is the dispatcher's own
    expansion, unlike original_router.routes (which loses include prefixes).
    Source: fastapi/fastapi tag 0.156.8, fastapi/routing.py, _IncludedRouter and
    _EffectiveRouteContext. Unexpected/low-priority route forms fail this smoke.
    """
    require(depth < 32)
    resolved = []
    for route in routes:
        kind = type(route)
        if kind.__module__ == "fastapi.routing" and kind.__name__ == "_IncludedRouter":
            # This application declares no frontend/low-priority routes; assert
            # that fact rather than silently omitting a second dispatch tier.
            require(not route.effective_low_priority_routes())
            resolved.extend(effective_routes(route.effective_candidates(), depth=depth + 1))
        else:
            if kind.__module__ == "fastapi.routing" and kind.__name__ == "_EffectiveRouteContext":
                route = route.starlette_route if route.starlette_route is not None else route
            require(isinstance(route.path, str) and route.path.startswith("/"))
            resolved.append(route)
    return resolved


async def compose_and_check():
    from fastapi import Request
    from services.partner_runtime import PartnerRuntimeSettings, build_runtime
    from services.partner_manager import manager_webhook_secret

    mocks = []

    def adapter(*methods, **values):
        for method in methods:
            values[method] = AsyncMock(side_effect=SmokeFailure)
            mocks.append(values[method])
        return SimpleNamespace(**values)

    pool, quote, summary = [AsyncMock(side_effect=SmokeFailure) for _ in range(3)]
    mocks.extend((pool, quote, summary))
    provider = adapter("create_invoice", "get_payment")
    provisioner = adapter("ensure_subscription", "read_usage")
    telegram = adapter("get_me", "get_webhook_info", "set_webhook", ready=True)
    sender = adapter("verify", "send")
    disabled = build_runtime(PartnerRuntimeSettings())
    report = disabled.settings.preflight()
    require(report["blockers"] == ["runtime_disabled"] and report["production_ready"] is False)
    require({route.path for route in effective_routes(disabled.app().routes)} == {"/healthz"})
    require(not disabled.worker().ready)
    require((await disabled.worker().run_once()) == {"enabled": False, "phase_errors": 0})

    flags = {name: True for name in (
        "enable_http", "enable_worker", "enable_billing", "invoice_intents_enabled",
        "panel_identity_enabled", "enable_provisioning", "enable_manager_webhook",
        "enable_customer_webhooks", "enable_launches", "enable_notifications",
        "enable_marketing_delivery", "enable_support_delivery", "enable_finance_notifications",
        "enable_public_gateway", "enable_public_checkout", "management_enabled",
        "finance_enabled", "branding_enabled", "allow_staging_network", "allow_staging_writes",
    )}
    settings = PartnerRuntimeSettings(
        mode="staging", staging_isolation_confirmed=True, database_isolation_confirmed=True,
        database_dsn="postgresql://synthetic:artificial@127.0.0.1/offline_smoke",
        database_tls=False, base_domain="partners.example.test",
        manager_origin="https://manager.example.test", webhook_origin="https://manager.example.test",
        manager_bot_token=TOKEN, manager_bot_username="OfflineManagerBot",
        manager_allowed_user_ids=(OWNER,), operator_ids=(90001,), admin_recipient_id=90001,
        encryption_keys=(base64.urlsafe_b64encode(b"v" * 32).decode(),),
        webhook_key_hex="b" * 64, subscription_key_hex="c" * 64,
        staging_no_addons_confirmed=True, yookassa_shop_id="123",
        yookassa_secret_key="artificial-test-merchant", remnawave_api_url="https://panel.example.test/api",
        remnawave_api_token="artificial-panel", remnawave_squad_ids=("00000000-0000-4000-8000-000000000001",),
        subscription_source_origin="https://source.example.test", **flags,
    )
    require(settings.preflight()["configured"])
    runtime = build_runtime(settings, pool, addon_loader=quote, catalog_loader=summary,
                            provider=provider, provisioner=provisioner, telegram=telegram, sender=sender)
    app = runtime.app()
    extensions = runtime.extensions()
    require(runtime.app() is app and runtime.extensions() is extensions)
    require(runtime.worker().ready and runtime.worker().runtime is runtime)
    require({name for name, _ in runtime._hooks} == {
        "marketing_delivery", "support_delivery", "finance_delivery"})
    require(len(runtime._hooks) == 3 and runtime.public_checkout.enabled)
    require(runtime.launcher.ready and runtime.recovery.ready and runtime.notifications.ready)
    routes = effective_routes(app.routes)
    paths = {route.path for route in routes}
    require({
        "/partner-manager/webhook", "/partner-bots/{project_id}/webhook",
        "/s/{token}", "/{token}", "/s/{token}/config", "/{token}/config",
        "/s/{token}/purchase-quote", "/{token}/purchases",
        "/partners/api/projects/{project_id}/entry",
        "/partners/workspace/{workspace_project_id}/api/projects/{project_id}/logo",
        "/partners/workspace/{workspace_project_id}/api/projects/{project_id}/finance/summary",
        "/partners/workspace/{workspace_project_id}/api/projects/{project_id}/management/team",
    } <= paths)
    require(not any("/operator/" in path for path in paths))
    core = next(route for route in routes
                if route.path == "/partners/api/projects" and "GET" in route.methods)
    authenticate = inspect.getclosurevars(core.endpoint).nonlocals["owner"]
    require(inspect.getclosurevars(authenticate).nonlocals["allowed_user_ids"] == (OWNER,))
    scope = {"type": "http", "headers": [(b"authorization", authorization(OWNER).encode())]}
    require(await authenticate(Request(scope)) == OWNER)

    project = "00000000-0000-4000-8000-000000000001"
    for path in (
        "/partners/api/projects", "/partners/api/settings",
    ):
        # Even an operator outside the owner allowlist must fail before DB use.
        status, _ = await request(app, path, headers=[(b"authorization", authorization(90001).encode())])
        require(status == 403)
    for path in (
        "/partners/api/management/projects",
        f"/partners/api/projects/{project}/finance/summary",
        f"/partners/api/projects/{project}/logo", "/partners/api/operator/withdrawals",
    ):
        status, _ = await request(app, path, headers=[(b"authorization", authorization(OWNER).encode())])
        require(status == 404)
    status, _ = await request(app, "/partners/api/projects")
    require(status == 401)
    manager = next(route for route in routes if route.path == "/partner-manager/webhook")
    handler = inspect.getclosurevars(manager.endpoint).nonlocals["handler"]
    require(handler.manager_allowed_user_ids == (OWNER,))
    for user_id, allowed in ((OWNER, True), (90001, False)):
        update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": user_id, "type": "private"},
                  "from": {"id": user_id, "is_bot": False}, "text": "/start"}}
        status, body = await request(app, "/partner-manager/webhook", method="POST", headers=[
            (b"content-type", b"application/json"),
            (b"x-telegram-bot-api-secret-token", manager_webhook_secret(bytes.fromhex("b" * 64), 123456789).encode()),
        ], body=json.dumps(update).encode())
        require(status == 200)
        result = json.loads(body)
        require(("reply_markup" in result) == allowed)
        if not allowed:
            require(result == {"ok": True})
    for mock in mocks:
        # Readiness may test a callable's truthiness; that is not adapter I/O.
        require(mock.call_count == 0 and mock.await_count == 0)
    return {"routes": len(paths), "worker_hooks": 3, "owner_allowlist": "enforced",
            "adapter_calls": 0, "default_mode": "disabled"}


def verify(release):
    sys.path.insert(0, str(release))
    guard = NoMainConfig()
    sys.meta_path.insert(0, guard)
    attempts = []
    saved_open, saved_io_open, saved_os_open = builtins.open, io.open, os.open

    def guarded_open(original, path, mode="r", *args, **kwargs):
        name = os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
        name = os.fsdecode(name)
        if any(value in mode for value in "wax+") or Path(name).suffix == ".json" or ".env" in Path(name).parts:
            attempts.append("file")
            raise SmokeFailure()
        return original(path, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            attempts.append("write")
            raise SmokeFailure()
        return saved_os_open(path, flags, *args, **kwargs)

    def deny(*args, **kwargs):
        attempts.append("network")
        raise SmokeFailure()

    try:
        with ExitStack() as stack:
            stack.enter_context(patch("builtins.open", side_effect=lambda *a, **k: guarded_open(saved_open, *a, **k)))
            stack.enter_context(patch("io.open", side_effect=lambda *a, **k: guarded_open(saved_io_open, *a, **k)))
            stack.enter_context(patch("os.open", side_effect=guarded_os_open))
            for target in ("socket.create_connection", "socket.getaddrinfo", "socket.gethostbyname",
                           "socket.socket.connect", "socket.socket.connect_ex", "socket.socket.bind", "socket.socket.listen"):
                stack.enter_context(patch(target, side_effect=deny))
            import aiohttp
            import asyncpg
            stack.enter_context(patch.object(aiohttp, "ClientSession", side_effect=deny))
            stack.enter_context(patch.object(asyncpg, "create_pool", side_effect=deny))
            stack.enter_context(patch.object(asyncpg, "connect", side_effect=deny))
            for name in MODULES:
                module = importlib.import_module(name)
                require(Path(module.__file__).resolve().is_relative_to(release.resolve()))
            result = asyncio.run(compose_and_check())
            require(not attempts)
            require(not {"config", "database", "dotenv"} & set(sys.modules))
            result.update(imported_modules=len(MODULES), forbidden_io_attempts=0)
            return result
    finally:
        sys.meta_path.remove(guard)
        sys.path.remove(str(release))


def main():
    try:
        require(os.geteuid() != 0 and pwd.getpwuid(os.geteuid()).pw_name == "spn-partner")
        require(RELEASE.is_dir() and not RELEASE.is_symlink() and RELEASE.stat().st_uid == 0)
        summary = verify(RELEASE)
        summary["versions"] = {name: importlib.metadata.version(name) for name in (
            "fastapi", "starlette", "pydantic", "aiohttp", "asyncpg", "uvicorn", "cryptography", "Pillow")}
        summary["ok"] = True
        print(json.dumps(summary, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error": "offline_staging_composition_smoke_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
