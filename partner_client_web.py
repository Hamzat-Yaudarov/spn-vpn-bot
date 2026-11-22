"""Opt-in partner-bot webhook factory, not mounted by the main SPN application.

Returning one Telegram API command in the webhook response avoids a token-bearing
outbound call. Delivery is not confirmed by this response; order/activation money
state is durable independently. No setWebhook call or project activation here.
"""
from __future__ import annotations

import hashlib
import asyncio
import hmac
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask


MAX_UPDATE_BYTES = 65_536


def webhook_secret(key: bytes, project_id, bot_id: int) -> str:
    if not isinstance(key, bytes) or not 32 <= len(key) <= 512:
        raise ValueError("Нужен отдельный ключ вебхуков не короче 32 байт.")
    if type(bot_id) is not int or not 0 < bot_id < 2**63:
        raise ValueError("Некорректный бот.")
    project_id = str(UUID(str(project_id)))
    return hmac.new(key, f"wl-webhook:v1:{project_id}:{bot_id}".encode(), hashlib.sha256).hexdigest()


async def _read_update(request):
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(415, "Нужен JSON-запрос.")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_UPDATE_BYTES:
            raise HTTPException(413, "Слишком большой запрос.")
    try:
        update = json.loads(raw)
        if (not isinstance(update, dict) or type(update.get("update_id")) is not int
                or not 0 <= update["update_id"] < 2**63):
            raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(400, "Некорректное обновление.") from None
    return update


def _lookup_user(update):
    """Untrusted lookup hint only; complete identity is checked by the handler."""
    part = update.get("callback_query", update.get("message"))
    sender = part.get("from") if isinstance(part, dict) else None
    value = sender.get("id") if isinstance(sender, dict) else None
    return value if type(value) is int and 0 < value < 2**63 else None


def create_client_router(project_loader, handler, *, secret_key: bytes = b"", enabled=False,
                         callback_acknowledger=None, context_loader=None):
    router = APIRouter()

    @router.post("/partner-bots/{project_id}/webhook", include_in_schema=False)
    async def telegram_update(project_id: str, request: Request):
        if not enabled:
            raise HTTPException(404, "Раздел недоступен.")
        try:
            project_id = str(UUID(project_id))
        except ValueError:
            raise HTTPException(404, "Бот недоступен.") from None
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if len(supplied) != 64 or any(c not in "0123456789abcdef" for c in supplied):
            raise HTTPException(403, "Недопустимый запрос.")
        # With the optimized registry port a bounded body is read before its
        # single SELECT, to join the exact customer's registration and staff
        # state. This does NOT authenticate the hint: no handling or side
        # effect happens until the project-specific webhook secret is checked.
        update = await _read_update(request) if context_loader is not None else None
        try:
            project = (
                await context_loader(project_id, _lookup_user(update))
                if context_loader is not None else await project_loader(project_id)
            )
        except LookupError:
            raise HTTPException(404, "Бот недоступен.") from None
        except Exception:
            raise HTTPException(503, "Бот временно недоступен.") from None
        try:
            if (str(project["id"]) != project_id or project["status"] != "active"
                    or not project["bot"]["id"]):
                raise ValueError()
            expected = webhook_secret(secret_key, project_id, project["bot"]["id"])
        except (ValueError, TypeError, KeyError):
            raise HTTPException(503, "Бот ещё не настроен.") from None
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(403, "Недопустимый запрос.")
        if update is None:
            update = await _read_update(request)
        ack = None
        if callback_acknowledger is not None and isinstance(update.get("callback_query"), dict):
            async def acknowledge():
                try:
                    await asyncio.wait_for(callback_acknowledger(project, update), timeout=3)
                except Exception:
                    pass
            # Start the UI-only acknowledgement alongside durable handling.
            # Its completion must not delay the visible webhook response; the
            # response's awaited background task owns it after the body flush.
            ack = asyncio.create_task(acknowledge())
        response_returned = False
        try:
            commands = await handler.handle(project, update)
            # A Telegram webhook response executes at most one API operation.
            # Prefer the visible screen; callback acknowledgement is secondary.
            visible = [cmd for cmd in commands if cmd.get("method") in {"sendMessage", "editMessageText"}]
            result = visible[-1] if visible else commands[-1] if commands else None
            if result is None:
                response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
            else:
                if (result["method"] not in {"sendMessage", "editMessageText", "answerCallbackQuery"}
                        or not isinstance(result.get("payload"), dict)
                        or "method" in result["payload"] or "token" in result["payload"]):
                    raise ValueError()
                response = JSONResponse({"method": result["method"], **result["payload"]},
                                        headers={"Cache-Control": "no-store"})
            if ack is not None:
                async def finish_acknowledgement():
                    await ack
                response.background = BackgroundTask(finish_acknowledgement)
            response_returned = True
            return response
        except Exception:
            # Telegram retries non-2xx; never acknowledge a failed durable write
            # as success or reflect a provider URL/token/SQL exception.
            raise HTTPException(503, "Обновление временно не обработано.") from None
        finally:
            if ack is not None and not response_returned:
                # No successful response owns this task (handler failed or the
                # request was cancelled). Stop UI-only work; the 503 still makes
                # Telegram retry the unsaved durable customer/order operation.
                ack.cancel()
                await asyncio.gather(ack, return_exceptions=True)

    return router
