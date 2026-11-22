"""Opt-in central manager webhook. Never mounted by the working SPN bot."""

import hmac
import json
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services.partner_launcher import _origin
from services.partner_manager import manager_webhook_secret

MAX_UPDATE_BYTES = 65_536


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def create_manager_router(
    handler, *, bot_id, secret_key=b"", webhook_origin="", enabled=False
):
    if type(enabled) is not bool:
        raise ValueError("Нужен явный флаг управляющего бота.")
    router = APIRouter()
    expected = manager_webhook_secret(secret_key, bot_id) if enabled else ""
    host = urlsplit(_origin(webhook_origin)).hostname if enabled else ""

    @router.post("/partner-manager/webhook", include_in_schema=False)
    async def manager_update(request: Request):
        if not enabled:
            raise HTTPException(404, "Раздел недоступен.")
        hosts = request.headers.getlist("host")
        secrets = request.headers.getlist("x-telegram-bot-api-secret-token")
        types = request.headers.getlist("content-type")
        if (
            len(hosts) != 1
            or hosts[0].lower() not in {host, host + ":443"}
            or request.url.query
        ):
            raise HTTPException(404, "Раздел недоступен.")
        if (
            len(secrets) != 1
            or not re.fullmatch(r"[a-f0-9]{64}", secrets[0])
            or not hmac.compare_digest(secrets[0], expected)
        ):
            raise HTTPException(403, "Недопустимый запрос.")
        if (
            len(types) != 1
            or types[0].split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise HTTPException(415, "Нужен JSON-запрос.")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_UPDATE_BYTES:
                raise HTTPException(413, "Слишком большой запрос.")
        try:
            update = json.loads(raw, object_pairs_hook=_unique)
            if (
                not isinstance(update, dict)
                or type(update.get("update_id")) is not int
                or not 0 <= update["update_id"] < 2**63
            ):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(400, "Некорректное обновление.") from None
        try:
            command = await handler.handle(update)
            return JSONResponse(
                command if command is not None else {"ok": True},
                headers={"cache-control": "no-store"},
            )
        except Exception:
            raise HTTPException(
                503, "Откройте управляющего бота немного позже."
            ) from None

    return router
