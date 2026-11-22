"""Owner-only logo JSON API; public tenant-bound image route lives in gateway."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from partner_finance_web import _object, _plain
from services.partner_brand_assets import (
    BrandAssetConflict,
    BrandAssetNotFound,
    BrandAssetUnavailable,
    InvalidBrandAsset,
    MAX_LOGO_BASE64,
)
from services.partner_web_auth import PrivateAPIRoute


async def _body(request, allowed):
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip()
        != "application/json"
    ):
        raise HTTPException(415, "Нужен JSON-запрос.")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_LOGO_BASE64 + 4096:
            raise HTTPException(413, "Логотип должен быть до 512 КБ.")
    try:
        value = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Некорректный JSON.") from None
    if not isinstance(value, dict) or set(value) != set(allowed):
        raise HTTPException(400, "Некорректные поля запроса.")
    return value


def create_router(store, *, authenticate, enabled=False, api_base="/partners/api"):
    if not callable(authenticate):
        raise TypeError("authenticate must verify central-manager initData")
    router = APIRouter(route_class=PrivateAPIRoute)

    async def owner(request):
        if enabled is not True:
            raise HTTPException(404, "Раздел недоступен.")
        actor = await authenticate(request)
        if type(actor) is not int or not 1 <= actor <= 2**63 - 1:
            raise HTTPException(401, "Откройте кабинет через управляющего бота.")
        return actor

    async def call(method, *args):
        try:
            return JSONResponse(_plain(await method(*args)))
        except BrandAssetNotFound:
            raise HTTPException(404, "Проект или логотип не найдены.") from None
        except BrandAssetConflict:
            raise HTTPException(409, "Настройки изменились. Обновите проект.") from None
        except InvalidBrandAsset as error:
            raise HTTPException(400, str(error)) from None
        except BrandAssetUnavailable:
            raise HTTPException(503, "Обработка логотипов пока недоступна.") from None
        except Exception:
            raise HTTPException(
                503, "Не удалось проверить загрузку. Обновите проект и повторите."
            ) from None

    base = api_base + "/projects/{project_id}/logo"

    @router.get(base)
    async def get_logo(project_id: str, request: Request):
        return await call(store.get_logo, project_id, await owner(request))

    @router.post(base)
    async def upload_logo(project_id: str, request: Request):
        actor = await owner(request)
        value = await _body(request, {"expected_version", "image_base64"})
        return await call(
            store.upload_logo,
            project_id,
            actor,
            value["expected_version"],
            value["image_base64"],
        )

    @router.delete(base)
    async def delete_logo(project_id: str, request: Request):
        actor = await owner(request)
        value = await _body(request, {"expected_version"})
        return await call(
            store.delete_logo, project_id, actor, value["expected_version"]
        )

    return router
