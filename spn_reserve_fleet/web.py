"""Authenticated loopback panel for preassigned reserve IPs; checks run off the UI loop."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


COOKIE = "spn_reserve_session"
STATIC = Path(__file__).with_name("static")
SERVER_FIELDS = ("id", "name", "domain", "current_ip", "ssh_port", "transport", "auto_enabled",
                 "status", "failures", "last_checked", "last_error", "pending_ip", "last_switch_target",
                 "last_error_code", "archived", "archive_reason", "replacement_id")
SERVER_FIELDS += ("operational_job_id", "cdn_domain")
RESERVE_FIELDS = ("ip", "status", "last_checked")
EVENT_FIELDS = ("id", "kind", "message", "created_at", "time", "server_id")


def public_server(row):
    result = {key: row[key] for key in SERVER_FIELDS if key in row}
    log_id = row.get("last_error_log_id")
    if isinstance(log_id, str) and re.fullmatch(r"[0-9a-f]{32}", log_id):
        result["last_error_log_id"] = log_id
    result["reserves"] = [{key: item[key] for key in RESERVE_FIELDS if key in item}
                          for item in row.get("reserves", [])]
    return result


def create_app(data_dir=None, *, runtime=None):
    if runtime is None:
        from .runtime import Runtime
        runtime = Runtime(data_dir)
    app = FastAPI(title="WAY SPN · Резервные IP", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    from .provisioning import Provisioner, CREATE_FIELDS, RESUME_FIELDS, public_job
    provisioner = Provisioner(runtime, getattr(runtime, 'env_file', None)) if hasattr(runtime, 'store') else None
    app.state.provisioning = provisioner

    def error(message, status=400):
        return JSONResponse({"message": message}, status_code=status)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.headers.get("content-length", "0").isdigit() and int(request.headers.get("content-length", "0")) > 65536:
            return error("Запрос слишком большой", 413)
        try:
            token, session = runtime.auth.session(request.cookies.get(COOKIE))
        except ValueError:
            return error("Невозможно открыть сессию панели", 503)
        request.state.token, request.state.session = token, session
        if request.method not in ("GET", "HEAD", "OPTIONS"):
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
        except (ValueError, KeyError):
            # Store/controller failures are deliberately scrubbed: provider and
            # deploy exceptions can otherwise contain private paths or tokens.
            response = error("Не удалось выполнить действие. Проверьте адрес и состояние сервера", 400)
        except Exception:
            response = error("Операция не завершена. Проверьте журнал программы", 500)
        if not getattr(request.state, "cookie_written", False):
            response.set_cookie(COOKIE, token, max_age=1800, httponly=True, samesite="strict",
                                secure=request.url.scheme == "https", path="/")
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    async def body(request, fields):
        raw = await request.body()
        if len(raw) > 65536:
            raise HTTPException(413, "Запрос слишком большой")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Нужен JSON-объект") from None
        if not isinstance(result, dict) or set(result) != set(fields):
            raise HTTPException(400, "Неверные поля запроса")
        return result

    def server(server_id, *, active=False):
        row = runtime.store.get_server(server_id)
        if not row:
            raise HTTPException(404, "Сервер не найден")
        if active and row.get("archived"):
            raise HTTPException(409, "Сервер в архиве. Управление отключено.")
        if active and row.get('operational_job_id'):
            raise HTTPException(409, 'Сначала завершите перенос VPS в разделе задач установки')
        return row

    @app.get('/api/provisioning/options')
    async def provisioning_options():
        return await asyncio.to_thread(provisioner.options)

    @app.post('/api/provisioning/ssh-key')
    async def provisioning_ssh_key(request: Request):
        data = await body(request, {'ip', 'ssh_port'})
        return await asyncio.to_thread(provisioner.ssh_key, data['ip'], data['ssh_port'])

    @app.get('/api/provisioning/jobs')
    def provisioning_jobs():
        return {'jobs': [public_job(job) for job in provisioner.jobs.list()[:100]]}

    @app.get('/api/provisioning/jobs/{job_id}')
    def provisioning_job(job_id: str):
        return public_job(provisioner.jobs.get(job_id))

    @app.post('/api/provisioning/jobs', status_code=202)
    async def provisioning_create(request: Request):
        data = await body(request, CREATE_FIELDS)
        if request.url.scheme != 'https':
            raise HTTPException(400, 'Для передачи пароля VPS откройте панель по HTTPS')
        try:
            return await asyncio.to_thread(provisioner.create, data)
        except ValueError as exc:
            # All validation messages are fixed strings from this module;
            # provider/installer exceptions never cross this API boundary.
            return error(str(exc), 400)
        finally:
            data['password'] = ''

    @app.post('/api/provisioning/jobs/{job_id}/resume', status_code=202)
    async def provisioning_resume(job_id: str, request: Request):
        data = await body(request, RESUME_FIELDS)
        if request.url.scheme != 'https':
            raise HTTPException(400, 'Для передачи пароля VPS откройте панель по HTTPS')
        try:
            return await asyncio.to_thread(provisioner.resume, job_id, data)
        except ValueError as exc:
            return error(str(exc), 400)
        finally:
            data['password'] = ''

    @app.get("/api/session")
    def session(request: Request):
        return {"authenticated": request.state.session["authenticated"], "csrf": request.state.session["csrf"],
                "configured": runtime.auth.configured()}

    @app.post("/api/login")
    async def login(request: Request):
        data = await body(request, {"password"})
        try:
            token, state = await asyncio.to_thread(runtime.auth.login, request.state.token, data["password"],
                                                  request.client.host if request.client else "unknown")
        except ValueError as exc:
            return error(str(exc), 401)
        response = JSONResponse({"authenticated": True, "csrf": state["csrf"]})
        response.set_cookie(COOKIE, token, max_age=1800, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")
        request.state.cookie_written = True
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        await body(request, set())
        runtime.auth.logout(request.state.token)
        request.state.cookie_written = True
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/overview")
    def overview():
        state = runtime.store.overview(include_archived=True)
        rows = state.get("servers", [])
        return {"servers": [public_server(row) for row in rows if not row.get("archived")],
                "archived_servers": [public_server(row) for row in rows if row.get("archived")],
                "events": [{key: row[key] for key in EVENT_FIELDS if key in row} for row in state.get("events", [])]}

    @app.post("/api/servers/{server_id}/check")
    async def check(server_id: str, request: Request):
        await body(request, set())
        server(server_id, active=True)
        await asyncio.to_thread(runtime.check, server_id)
        return public_server(server(server_id))

    @app.post("/api/servers/{server_id}/switch")
    async def switch(server_id: str, request: Request):
        data = await body(request, {"ip"})
        row = server(server_id, active=True)
        if row.get("pending_ip") and data["ip"] != row["pending_ip"]:
            raise HTTPException(409, "Продолжите незавершённую смену на адрес, указанный в карточке")
        if not isinstance(data["ip"], str) or data["ip"] not in {item["ip"] for item in row.get("reserves", [])}:
            raise HTTPException(400, "Выберите IP из резерва этого сервера")
        await asyncio.to_thread(runtime.switch, server_id, data["ip"])
        return public_server(server(server_id))

    @app.put("/api/servers/{server_id}/auto")
    async def automatic(server_id: str, request: Request):
        data = await body(request, {"enabled"})
        if not isinstance(data["enabled"], bool):
            raise HTTPException(400, "Нужно логическое значение enabled")
        row = server(server_id, active=True)
        if data["enabled"] and row.get("pending_ip"):
            raise HTTPException(409, "Сначала завершите текущую смену IP")
        return public_server(await asyncio.to_thread(runtime.store.set_auto, server_id, data["enabled"]))

    @app.post("/api/servers/{server_id}/reserves")
    async def add_reserve(server_id: str, request: Request):
        data = await body(request, {"ip"})
        server(server_id, active=True)
        if not isinstance(data["ip"], str):
            raise HTTPException(400, "Нужен IPv4")
        return public_server(await asyncio.to_thread(runtime.store.add_reserve, server_id, data["ip"]))

    @app.delete("/api/servers/{server_id}/reserves/{ip}")
    async def remove_reserve(server_id: str, ip: str, request: Request):
        await body(request, set())
        server(server_id, active=True)
        return public_server(await asyncio.to_thread(runtime.store.remove_reserve, server_id, ip))

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
