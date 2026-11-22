"""Opt-in finance API with injected owner or central-operator authentication."""

from __future__ import annotations

from datetime import date, datetime
import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services.partner_finance import (
    FinanceConflict,
    FinanceForbidden,
    FinanceNotFound,
    FinanceUnavailable,
    InvalidFinance,
)
from services.partner_web_auth import PrivateAPIRoute


MAX_BODY = 8192


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


async def _body(request, *, required, optional=()):
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip()
        != "application/json"
    ):
        raise HTTPException(415, "Нужен JSON-запрос.")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BODY:
            raise HTTPException(413, "Слишком большой запрос.")
    try:
        value = json.loads(content, object_pairs_hook=_object)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Некорректный JSON.") from None
    if (
        not isinstance(value, dict)
        or set(required) - value.keys()
        or value.keys() - set(required) - set(optional)
    ):
        raise HTTPException(400, "Некорректные поля запроса.")
    return value


def create_router(store, *, authenticate, enabled=False, api_base="/partners/api", allow_operator=True):
    """Operator routes require verified CENTRAL manager identity.

    Tenant composition instead injects exact-project owner authentication and
    MUST pass allow_operator=False. Global operator identity cannot be proved by
    an owner-controlled tenant token. Stores repeat owner/operator authorization.
    No body/header-selected admin ID, token or merchant is trusted.
    """
    if not callable(authenticate):
        raise TypeError("authenticate must be callable")
    router = APIRouter(route_class=PrivateAPIRoute)

    async def actor(request, *, operator=False):
        if enabled is not True:
            raise HTTPException(404, "Раздел недоступен.")
        identity = await authenticate(request)
        if type(identity) is not int or not 1 <= identity <= 2**63 - 1:
            raise HTTPException(401, "Откройте кабинет через управляющего бота.")
        if operator:
            try:
                store.require_operator(identity)
            except FinanceForbidden:
                raise HTTPException(403, "Недостаточно прав.") from None
            except FinanceUnavailable:
                raise HTTPException(503, "Финансы ещё не настроены.") from None
        return identity

    async def call(method, *args, **kwargs):
        try:
            result = await method(*args, **kwargs)
            return JSONResponse(
                _plain(result),
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except FinanceNotFound:
            raise HTTPException(404, "Проект или операция не найдены.") from None
        except FinanceForbidden:
            raise HTTPException(403, "Недостаточно прав.") from None
        except InvalidFinance as error:
            raise HTTPException(400, str(error)) from None
        except FinanceConflict as error:
            return JSONResponse(
                {"detail": str(error), "code": error.code},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        except FinanceUnavailable:
            raise HTTPException(
                503, "Финансы ещё не настроены или временно недоступны."
            ) from None
        except Exception:
            # Database/crypto failures may include ciphertext/requisites.
            raise HTTPException(
                503, "Операцию не удалось проверить. Повторите с тем же ключом запроса."
            ) from None

    base = api_base + "/projects/{project_id}/finance"

    @router.get(base + "/summary")
    async def summary(project_id: str, request: Request):
        return await call(store.summary, project_id, await actor(request))

    @router.get(base + "/withdrawals")
    async def withdrawals(project_id: str, request: Request):
        return await call(store.list_withdrawals, project_id, await actor(request))

    @router.get(base + "/reversals")
    async def reversals(project_id: str, request: Request):
        return await call(store.list_reversals, project_id, await actor(request))

    @router.post(base + "/withdrawals")
    async def create_withdrawal(project_id: str, request: Request):
        owner = await actor(request)
        body = await _body(
            request, required={"request_key", "amount_kopecks", "method", "requisites"}
        )
        return await call(
            store.request_withdrawal,
            project_id,
            owner,
            body["request_key"],
            body["amount_kopecks"],
            body["method"],
            body["requisites"],
        )

    @router.get(base + "/withdrawals/{request_key}")
    async def withdrawal(project_id: str, request_key: str, request: Request):
        return await call(
            store.get_withdrawal, project_id, await actor(request), request_key
        )

    @router.get(api_base + "/operator/withdrawals")
    async def operator_withdrawals(request: Request):
        return await call(
            store.list_operator_withdrawals, await actor(request, operator=True)
        )

    operator_base = (
        api_base + "/operator/projects/{project_id}/withdrawals/{withdrawal_id}"
    )

    @router.get(operator_base)
    async def operator_details(project_id: str, withdrawal_id: str, request: Request):
        return await call(
            store.operator_details,
            project_id,
            withdrawal_id,
            await actor(request, operator=True),
        )

    @router.post(operator_base + "/claim")
    async def operator_claim(project_id: str, withdrawal_id: str, request: Request):
        operator = await actor(request, operator=True)
        await _body(request, required=set())
        return await call(store.claim_withdrawal, project_id, withdrawal_id, operator)

    @router.post(operator_base + "/paid")
    async def operator_paid(project_id: str, withdrawal_id: str, request: Request):
        operator = await actor(request, operator=True)
        body = await _body(
            request,
            required={
                "actual_amount_kopecks",
                "transfer_reference",
                "confirmed_transferred",
            },
        )
        if body["confirmed_transferred"] is not True:
            raise HTTPException(
                400, "Подтвердите факт уже выполненного ручного перевода."
            )
        return await call(
            store.confirm_withdrawal_paid,
            project_id,
            withdrawal_id,
            operator,
            body["actual_amount_kopecks"],
            body["transfer_reference"],
        )

    @router.post(operator_base + "/reject")
    async def operator_reject(project_id: str, withdrawal_id: str, request: Request):
        operator = await actor(request, operator=True)
        body = await _body(
            request, required={"reason"}, optional={"confirmed_no_transfer"}
        )
        return await call(
            store.reject_withdrawal,
            project_id,
            withdrawal_id,
            operator,
            body["reason"],
            confirmed_no_transfer=body.get("confirmed_no_transfer", False),
        )

    @router.post(api_base + "/operator/projects/{project_id}/reversals")
    async def operator_reversal(project_id: str, request: Request):
        operator = await actor(request, operator=True)
        body = await _body(
            request,
            required={
                "order_id",
                "request_key",
                "amount_kopecks",
                "kind",
                "event_reference",
                "reason",
                "confirmed_external_event",
            },
        )
        if body["confirmed_external_event"] is not True:
            raise HTTPException(
                400, "Учитывается только подтверждённое внешнее событие."
            )
        return await call(
            store.confirm_reversal,
            project_id,
            body["order_id"],
            operator,
            body["request_key"],
            body["amount_kopecks"],
            kind=body["kind"],
            event_reference=body["event_reference"],
            reason=body["reason"],
        )

    if not allow_operator:
        router.routes = [route for route in router.routes if not route.path.startswith(api_base + "/operator/")]
    return router
