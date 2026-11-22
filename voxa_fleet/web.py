"""Loopback-first authenticated admin API. Money only leaves via the worker."""
from __future__ import annotations

import json
from pathlib import Path
import uuid
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .provider import ProviderError
from .runtime import Runtime
from .store import ConflictError


COOKIE = "spn_fleet_session"
STATIC = Path(__file__).with_name("static")


def create_app(data_dir=None, *, runtime=None):
    runtime = runtime or Runtime(data_dir)
    app = FastAPI(title="SPN Voxa Fleet", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime

    def error(message, status=400):
        return JSONResponse({"message": message}, status_code=status)

    @app.middleware("http")
    async def security(request: Request, call_next):
        parsed_host = request.url.hostname
        if not parsed_host:
            return error("Отсутствует адрес запроса", 400)
        if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > 65536:
            return error("Запрос слишком большой", 413)
        token, session = runtime.auth.session(request.cookies.get(COOKIE))
        request.state.token, request.state.session = token, session
        mutating = request.method not in ("GET", "HEAD", "OPTIONS")
        if mutating:
            origin = request.headers.get("origin")
            if origin:
                source = urlsplit(origin)
                if source.scheme != request.url.scheme or source.netloc.lower() != request.url.netloc.lower():
                    return error("Запрос должен исходить из этой панели", 403)
            if not runtime.auth.csrf_valid(session, request.headers.get("x-csrf-token")):
                return error("Сессия формы истекла. Обновите страницу", 403)
        if request.url.path.startswith("/api/") and request.url.path not in ("/api/session", "/api/login"):
            if session.get("authenticated") is not True:
                return error("Нужен вход в панель", 401)
        try:
            response = await call_next(request)
        except ConflictError as exc:
            response = error(str(exc), 409)
        except (ValueError, ProviderError) as exc:
            response = error(str(exc), 400)
        except Exception:
            response = error("Операция не выполнена. Подробности скрыты для защиты ключей", 500)
        # A newly authenticated route replaces the guest token itself.
        if not getattr(request.state, "cookie_written", False):
            response.set_cookie(COOKIE, token, max_age=1800, httponly=True, samesite="strict",
                                secure=request.url.scheme == "https", path="/")
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    async def body(request):
        raw = await request.body()
        if len(raw) > 65536:
            raise ValueError("Запрос слишком большой")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Нужен JSON-объект")
        return result

    @app.get("/api/session")
    def session(request: Request):
        return {"authenticated": request.state.session["authenticated"], "csrf": request.state.session["csrf"],
                "configured": runtime.auth.configured()}

    @app.post("/api/login")
    async def login(request: Request):
        data = await body(request)
        token, state = runtime.auth.login(request.state.token, data.get("password"), request.client.host if request.client else "unknown")
        response = JSONResponse({"authenticated": True, "csrf": state["csrf"]})
        response.set_cookie(COOKIE, token, max_age=1800, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")
        request.state.cookie_written = True
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        runtime.auth.logout(request.state.token)
        request.state.cookie_written = True
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/overview")
    def overview():
        return runtime.overview()

    @app.get("/api/servers")
    def servers():
        return runtime.public_servers()

    @app.post("/api/servers")
    async def create_server(request: Request):
        key = str(uuid.UUID(request.headers.get("x-request-id", str(uuid.uuid4()))))
        return runtime.create_server(await body(request), key)

    @app.patch("/api/servers/{server_id}")
    async def update_server(server_id: str, request: Request):
        changes = await body(request)
        if set(changes) - {"name", "mode", "enabled", "product_id", "location"}:
            raise ValueError("Эти поля нельзя менять через форму сервера")
        return runtime.store.update_server(server_id, changes)

    @app.post("/api/servers/{server_id}/actions/{action}")
    async def server_action(server_id: str, action: str, request: Request):
        if not runtime.store.get_server(server_id):
            raise HTTPException(404, "Сервер не найден")
        if action in ("pause", "resume"):
            return runtime.store.update_server(server_id, {"enabled": action == "resume"})
        if action not in ("check", "replace"):
            raise ValueError("Неизвестное действие")
        key = str(uuid.UUID(request.headers.get("x-request-id", str(uuid.uuid4()))))
        row = runtime.store.enqueue_operation(action, server_id, {"approved": action == "replace"}, idempotency_key=key)
        return {key: row[key] for key in ("id", "phase", "action", "server_id")}

    @app.get("/api/operations")
    def operations():
        return runtime.public_operations()

    @app.get("/api/events")
    def events(operation_id: str | None = None):
        rows = runtime.store.list_events(limit=200)
        # Never return arbitrary internal event payloads or provider data.
        return [{key: row[key] for key in ("id", "kind", "message", "created_at", "operation_id", "server_id") if key in row}
                for row in rows if operation_id is None or row.get("operation_id") == operation_id]

    @app.get("/api/settings")
    def settings():
        return runtime.store.get_settings()

    @app.put("/api/settings")
    async def update_settings(request: Request):
        changes = await body(request)
        if changes.get("financial_enabled") is False:
            changes["auto_enabled"] = False
        return runtime.store.update_settings(changes, user_action=True)

    @app.post("/api/credentials")
    async def credentials(request: Request):
        if request.url.scheme != "https" and request.url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Ключи можно вводить только через HTTPS или локальный SSH-туннель")
        result = runtime.credentials.update(await body(request))
        runtime.cache.update(identity=None, preflight_at=None, products=[], catalog_at=None, balance=None)
        runtime.store.add_event("credentials.updated", "Защищённые настройки обновлены; значения не записываются в журнал")
        return result

    @app.get("/api/catalog")
    def catalog():
        return runtime.catalog()

    @app.post("/api/catalog/refresh")
    def refresh_catalog():
        return runtime.catalog(refresh=True)

    @app.post("/api/preflight")
    def preflight():
        return runtime.preflight()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
