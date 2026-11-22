"""Opt-in same-origin bearer-link renewal API; no cookie/Telegram ID identity."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services.partner_checkout import CheckoutError, InvoiceRecoveryRequired
from services.partner_commerce_store import (
    CommerceConflict,
    CommerceNotFound,
    InvalidCommerce,
    SalesUnavailable,
)
from services.partner_public_checkout import (
    InvalidPublicCheckout,
    PublicCheckoutConflict,
    PublicCheckoutUnavailable,
)
from services.partner_subscriptions import SubscriptionNotFound, SubscriptionUnavailable
from services.partner_web_auth import PrivateAPIRoute


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _plain(value):
    from datetime import datetime, date
    from uuid import UUID

    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value) if isinstance(value, UUID) else value


async def _body(request):
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip()
        != "application/json"
    ):
        raise HTTPException(415, "Нужен JSON-запрос.")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 4096:
            raise HTTPException(413, "Слишком большой запрос.")
    try:
        value = json.loads(data, object_pairs_hook=_object)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Некорректный JSON.") from None
    if not isinstance(value, dict) or set(value) != {
        "request_key",
        "expected_price_kopecks",
    }:
        raise HTTPException(400, "Некорректные поля запроса.")
    return value


def create_router(service, *, enabled=False):
    router = APIRouter(route_class=PrivateAPIRoute)

    def request_slug(request, *, mutation=False):
        if enabled is not True:
            raise HTTPException(404, "Раздел недоступен.")
        hosts = request.headers.getlist("host")
        try:
            if len(hosts) != 1 or request.url.query:
                raise SubscriptionNotFound()
            slug = service.links.slug_from_host(hosts[0])
        except (SubscriptionNotFound, ValueError):
            raise HTTPException(404, "Подписка не найдена.") from None
        if mutation:
            origins = request.headers.getlist("origin")
            try:
                if len(origins) != 1:
                    raise ValueError()
                origin = urlsplit(origins[0])
                canonical_host = hosts[0].lower().removesuffix(":443")
                if (
                    origin.scheme != "https"
                    or origin.hostname != canonical_host
                    or origin.port not in (None, 443)
                    or origin.username
                    or origin.password
                    or origin.path
                    or origin.query
                    or origin.fragment
                    or any(c.isspace() for c in origins[0])
                    or "\\" in origins[0]
                ):
                    raise ValueError()
                sites = request.headers.getlist("sec-fetch-site")
                if sites and (len(sites) != 1 or sites[0] != "same-origin"):
                    raise ValueError()
            except ValueError:
                raise HTTPException(
                    403, "Оплату можно создать только с вашей страницы подписки."
                ) from None
        return slug

    async def call(method, *args):
        try:
            return JSONResponse(_plain(await method(*args)))
        except (SubscriptionNotFound, CommerceNotFound):
            raise HTTPException(404, "Подписка или покупка не найдены.") from None
        except PublicCheckoutConflict as error:
            return JSONResponse(
                {"detail": str(error), "code": error.code}, status_code=409
            )
        except InvoiceRecoveryRequired:
            return JSONResponse(
                {
                    "detail": "Нужна проверка сохранённого счёта поддержкой. Повторно платить не нужно.",
                    "code": "invoice_manual_review",
                },
                status_code=409,
            )
        except (CommerceConflict, SalesUnavailable):
            raise HTTPException(
                409,
                "Сначала завершите сохранённую покупку или обратитесь в поддержку. Повторно платить не нужно.",
            ) from None
        except (InvalidPublicCheckout, InvalidCommerce):
            raise HTTPException(
                400, "Проверьте сумму и идентификатор покупки."
            ) from None
        except (PublicCheckoutUnavailable, SubscriptionUnavailable, CheckoutError):
            raise HTTPException(
                503,
                "Покупка пока не подтверждена. Повторите проверку с тем же ключом; повторно платить не нужно.",
            ) from None
        except Exception:
            raise HTTPException(
                503,
                "Не удалось проверить покупку. Сохраните страницу и повторите проверку.",
            ) from None

    @router.get("/s/{token}/purchase-quote")
    @router.get("/{token}/purchase-quote")
    async def quote(request: Request, token: str):
        return await call(service.quote, request_slug(request), token)

    @router.post("/s/{token}/purchases")
    @router.post("/{token}/purchases")
    async def purchase(request: Request, token: str):
        slug = request_slug(request, mutation=True)
        body = await _body(request)
        return await call(
            service.create_purchase,
            slug,
            token,
            body["request_key"],
            body["expected_price_kopecks"],
        )

    @router.get("/s/{token}/purchases/{request_key}")
    @router.get("/{token}/purchases/{request_key}")
    async def state(request: Request, token: str, request_key: str):
        return await call(
            service.get_purchase, request_slug(request), token, request_key
        )

    return router
