"""Disabled-by-default project management API with injected identity boundary.

This router never accepts a user ID, role or token as authentication. Customer
consent and first-start attribution are trusted webhook ports, not public admin
endpoints. Tenant composition must use project-bound authentication and must not
record a global manager identity. It does not send messages, grant service, or move money.
"""

from __future__ import annotations

import inspect
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from services.partner_store import InvalidProject
from services.partner_management import ManagementConflict, ManagementDenied
from services.partner_web_auth import PrivateAPIRoute


def create_management_router(store, authenticate, *, enabled=False, api_base="/partners/api", workspace_scope=False):
    if not callable(authenticate) or type(enabled) is not bool:
        raise ValueError(
            "Нужна проверка центрального Telegram-клиента и явный флаг API."
        )
    router = APIRouter(route_class=PrivateAPIRoute)
    prefix = api_base + "/projects/{project_id}/management"

    async def actor(request):
        if not enabled:
            raise HTTPException(404, "Раздел недоступен.")
        value = authenticate(request)
        value = await value if inspect.isawaitable(value) else value
        if type(value) is not int or value <= 0:
            raise HTTPException(401, "Откройте кабинет через Telegram.")
        if not workspace_scope:
            try:
                await store.record_manager_identity(value)
            except Exception:
                raise HTTPException(
                    503, "Не удалось проверить профиль управляющего кабинета."
                ) from None
        return value

    async def body(request, allowed, required):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip()
            != "application/json"
        ):
            raise HTTPException(415, "Нужен JSON-запрос.")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 16384:
                raise HTTPException(413, "Слишком большой запрос.")

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value

        try:
            value = json.loads(data, object_pairs_hook=unique)
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Некорректный JSON.") from None
        if (
            not isinstance(value, dict)
            or set(value) - set(allowed)
            or set(required) - set(value)
        ):
            raise HTTPException(400, "Некорректные поля запроса.")
        return value

    async def call(operation):
        try:
            value = await operation
        except InvalidProject as exc:
            raise HTTPException(400, str(exc)) from None
        except ManagementDenied:
            raise HTTPException(404, "Проект или действие недоступны.") from None
        except ManagementConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            # Database/transport failures are not a successful empty report and
            # must not leak SQL, private parameters or encrypted bot material.
            raise HTTPException(
                503, "Не удалось проверить действие. Повторите с тем же ключом запроса."
            ) from None
        return JSONResponse(
            jsonable_encoder(value), headers={"Cache-Control": "no-store"}
        )

    @router.get(api_base + "/management/projects")
    async def projects(request: Request):
        who = await actor(request)
        if workspace_scope:
            return JSONResponse(jsonable_encoder({"items": [request.state.partner_workspace["project"]], "truncated": False}))
        return await call(store.visible_projects(who))

    @router.get(prefix + "/access")
    async def access(project_id: str, request: Request):
        who = await actor(request)
        # Every permitted role has referrals; no privilege union with other tenants.
        return await call(store.authorize(project_id, who, "referrals"))

    @router.get(prefix + "/overview")
    async def overview(project_id: str, request: Request):
        return await call(store.overview(project_id, await actor(request)))

    @router.get(prefix + "/customers")
    async def customers(
        project_id: str,
        request: Request,
        query: str = "",
        limit: int = 50,
        cursor: int = 0,
    ):
        return await call(
            store.customers(
                project_id,
                await actor(request),
                query=query,
                limit=limit,
                cursor=cursor,
            )
        )

    @router.get(prefix + "/customers/{tg_id}/orders")
    async def customer_orders(
        project_id: str, tg_id: int, request: Request, limit: int = 50, cursor: int = 0
    ):
        return await call(
            store.orders(
                project_id,
                await actor(request),
                tg_id=tg_id,
                limit=limit,
                cursor=cursor,
            )
        )

    @router.post(prefix + "/customers/{tg_id}/resend")
    async def resend(project_id: str, tg_id: int, request: Request):
        who = await actor(request)
        value = await body(request, {"request_key"}, {"request_key"})
        return await call(
            store.resend_subscription(
                project_id, who, value["request_key"], tg_id=tg_id
            )
        )

    @router.get(prefix + "/resends/{intent_id}")
    async def resend_state(project_id: str, intent_id: str, request: Request):
        return await call(store.get_resend(project_id, await actor(request), intent_id))

    @router.get(prefix + "/subscriptions")
    async def subscriptions(
        project_id: str, request: Request, limit: int = 50, cursor: int = 0
    ):
        return await call(
            store.subscriptions(
                project_id, await actor(request), limit=limit, cursor=cursor
            )
        )

    @router.get(prefix + "/orders")
    async def orders(
        project_id: str, request: Request, limit: int = 50, cursor: int = 0
    ):
        return await call(
            store.orders(project_id, await actor(request), limit=limit, cursor=cursor)
        )

    @router.get(prefix + "/referrals")
    async def referrals(project_id: str, request: Request):
        return await call(store.referrals(project_id, await actor(request)))

    @router.get(prefix + "/team")
    async def team(project_id: str, request: Request):
        return await call(store.team(project_id, await actor(request)))

    @router.post(prefix + "/team")
    async def member(project_id: str, request: Request):
        who = await actor(request)
        value = await body(
            request,
            {"request_key", "tg_id", "role", "expected_version"},
            {"request_key", "tg_id", "role", "expected_version"},
        )
        return await call(store.set_member(project_id, who, **value))

    @router.get(prefix + "/campaigns")
    async def campaigns(
        project_id: str, request: Request, limit: int = 50, cursor: int = 0
    ):
        return await call(
            store.campaigns(
                project_id, await actor(request), limit=limit, cursor=cursor
            )
        )

    @router.post(prefix + "/campaigns")
    async def campaign(project_id: str, request: Request):
        who = await actor(request)
        value = await body(
            request,
            {"request_key", "title", "campaign_id", "expected_version", "status"},
            {"request_key", "title"},
        )
        return await call(store.save_campaign(project_id, who, **value))

    @router.get(prefix + "/broadcasts")
    async def broadcasts(
        project_id: str, request: Request, limit: int = 50, cursor: int = 0
    ):
        return await call(
            store.broadcasts(
                project_id, await actor(request), limit=limit, cursor=cursor
            )
        )

    @router.post(prefix + "/broadcasts")
    async def broadcast(project_id: str, request: Request):
        who = await actor(request)
        value = await body(
            request,
            {"request_key", "text", "broadcast_id", "expected_version"},
            {"request_key", "text"},
        )
        return await call(store.save_broadcast(project_id, who, **value))

    @router.post(prefix + "/broadcasts/{broadcast_id}/review")
    async def review(project_id: str, broadcast_id: str, request: Request):
        who = await actor(request)
        value = await body(request, {"expected_version"}, {"expected_version"})
        return await call(
            store.review_broadcast(project_id, who, broadcast_id, **value)
        )

    @router.post(prefix + "/broadcasts/{broadcast_id}/confirm")
    async def confirm(project_id: str, broadcast_id: str, request: Request):
        who = await actor(request)
        value = await body(
            request,
            {"request_key", "expected_version", "review_token"},
            {"request_key", "expected_version", "review_token"},
        )
        return await call(
            store.confirm_broadcast(project_id, who, broadcast_id=broadcast_id, **value)
        )

    @router.post(prefix + "/broadcasts/{broadcast_id}/cancel")
    async def cancel(project_id: str, broadcast_id: str, request: Request):
        who = await actor(request)
        value = await body(
            request,
            {"request_key", "expected_version"},
            {"request_key", "expected_version"},
        )
        return await call(
            store.cancel_broadcast(project_id, who, broadcast_id=broadcast_id, **value)
        )

    return router
