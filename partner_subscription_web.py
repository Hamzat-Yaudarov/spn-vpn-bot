"""Dedicated public subscription router. NOT mounted by the working bot.

No query parameters, forwarded-Host trust, redirects to upstream credentials,
external trackers, cookies or browser fetches. The reverse proxy must suppress
capability paths in logs (see the separate nginx example).
"""

from html import escape
from pathlib import Path
import re
import secrets
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from services.partner_store import _brand, _text, _url
from services.partner_brand_defaults import DEFAULT_CUSTOMER_SUPPORT_URL
from services.partner_subscriptions import (
    CLIENT_HEADERS,
    SubscriptionExpired,
    SubscriptionNotFound,
    SubscriptionUnavailable,
)

ASSETS = Path(__file__).resolve().parent / "static" / "partner-subscription" / "assets"
VERSION = "20261022-autocheck1"
BASE_HEADERS = {
    "cache-control": "private, no-store, max-age=0",
    "pragma": "no-cache",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "x-robots-tag": "noindex, nofollow, noarchive",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def security_headers(nonce=None, *, checkout=False):
    return {
        **BASE_HEADERS,
        "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self' "
        + (f"'nonce-{nonce}'; " if nonce else "; ")
        + ("connect-src 'self'; " if checkout else "connect-src 'none'; ")
        + "img-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'",
    }


def _foreground(accent):
    rgb = [int(accent[n : n + 2], 16) / 255 for n in (1, 3, 5)]
    rgb = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    luminance = sum(v * w for v, w in zip(rgb, (0.2126, 0.7152, 0.0722)))
    return "#000000" if (luminance + 0.05) / 0.05 >= 4.5 else "#ffffff"


def _gb(value):
    return f"{value / 1024**3:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " ГБ"


def render_subscription_page(context, links, state, quota, *, checkout=False):
    """Escaped, server-rendered data only; no source URL or Telegram ID in HTML."""
    title = _text(context["title"], "title", maximum=80)
    brand = _brand(context.get("brand", {}))
    accent = brand.get("accent_color", "#d8b55b")
    heading = brand.get("subscription_title") or title
    description = (
        brand.get("subscription_description") or "Ваша подписка и подключение к VPN."
    )
    username = context.get("bot_username")
    bot_url = (
        "https://t.me/" + username
        if isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
        else ""
    )
    support = _url(context.get("support_url") or DEFAULT_CUSTOMER_SUPPORT_URL, "support_url")
    expires = context["expires_at"].strftime("%d.%m.%Y")
    status, explanation = {
        "ready": ("Подписка активна", "Это статус подписки, а не VPN-соединения."),
        "expired": (
            "Срок подписки истёк",
            "Продлите подписку в боте, затем обновите её в приложении.",
        ),
        "updating": (
            "Оплата сохранена",
            "Изменения ещё применяются. Повторно платить не нужно.",
        ),
        "unavailable": (
            "Данные временно недоступны",
            "Не удалось проверить трафик. Ваши ГБ не сброшены. Обновите страницу позже.",
        ),
    }[state]
    ready = state == "ready" and isinstance(quota, dict)
    quota_html = ""
    if ready:
        quota_html = (
            f'<div class="metric"><dt>Осталось трафика</dt><dd>{_gb(quota["remaining_bytes"])}</dd>'
            f'<p>Использовано {_gb(quota["used_bytes"])} из {_gb(quota["total_bytes"])}</p></div>'
        )
    else:
        quota_html = '<div class="metric"><dt>Трафик</dt><dd class="small">Уточняется</dd><p>Данные появятся после проверки.</p></div>'
    devices = context.get("device_limit")
    device_text = (
        str(devices) if type(devices) is int and 3 <= devices <= 10 else "Уточняется"
    )
    config_url, page_url = links.import_url(context), links.url(context)
    connection = ""
    if ready:
        happ = "happ://add/" + quote(
            config_url, safe=""
        )  # Existing main MiniApp import format.
        connection = f"""<section class="card" aria-labelledby="connect-title">
          <h2 id="connect-title">Подключение</h2>
          <a class="button primary" href="{escape(happ, quote=True)}">Открыть в Happ</a>
          <button class="button" id="copy-link" type="button">Скопировать ссылку</button>
          <p class="hint">Для INCY скопируйте ссылку и добавьте подписку из буфера обмена.</p>
          <details><summary>Личная ссылка</summary><label for="subscription-link">Не пересылайте её другим людям</label>
            <input id="subscription-link" readonly spellcheck="false" value="{escape(config_url, quote=True)}" aria-label="Личная ссылка подписки"></details>
          <p id="copy-status" role="status" aria-live="polite"></p>
        </section>"""
    else:
        connection = '<section class="card"><h2>Подключение</h2><p>Импорт станет доступен после подтверждения актуальных данных подписки.</p></section>'
    actions = ""
    if bot_url:
        actions += f'<a class="button" href="{escape(bot_url, quote=True)}" rel="noreferrer noopener">Продлить и докупить в боте</a>'
    if support:
        actions += f'<a class="button" href="{escape(support, quote=True)}" rel="noreferrer noopener">Поддержка</a>'
    if state in {"updating", "unavailable"}:
        actions += f'<a class="button" href="{escape(page_url, quote=True)}">Обновить страницу</a>'
    nonce = secrets.token_urlsafe(18)
    logo = brand.get("logo_asset_id")
    mark = (
        f'<img class="mark" src="/partner-subscription-assets/logo/{logo}.png" alt="">'
        if logo
        else f'<span class="mark" aria-hidden="true">{escape(title[:1])}</span>'
    )
    renewal = (
        f'<section class="card" id="renewal" data-purchase-path="{escape(urlsplit(page_url).path, quote=True)}" aria-labelledby="renewal-title">'
        '<h2 id="renewal-title">Продлить подписку</h2><div id="renewal-content"><p role="status">Загружаем стоимость…</p></div></section>'
        if checkout
        else ""
    )
    body = f"""<!doctype html><html lang="ru"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
      <meta name="robots" content="noindex,nofollow"><title>{escape(heading)}</title>
      <link rel="stylesheet" href="/partner-subscription-assets/subscription.css?v={VERSION}">
      <style nonce="{nonce}">:root{{--accent:{accent};--on-accent:{_foreground(accent)}}}</style>
      <script defer src="/partner-subscription-assets/subscription.js?v={VERSION}"></script></head>
      <body><main><header>{mark}
        <p class="brand">{escape(title)}</p><h1>{escape(heading)}</h1><p>{escape(description)}</p></header>
        <section class="card" aria-labelledby="status-title"><h2 id="status-title">{status}</h2>
          <p class="hint">{explanation}</p><dl><div class="metric"><dt>Действует до</dt><dd>{expires}</dd></div>
          {quota_html}<div class="metric"><dt>Лимит устройств</dt><dd>{device_text}</dd></div></dl></section>
        {connection}{renewal}<section class="actions" aria-label="Управление подпиской">{actions}</section>
        <footer>Включайте VPN в приложении после добавления подписки.</footer></main></body></html>"""
    return HTMLResponse(body, headers=security_headers(nonce, checkout=checkout))


def wants_html(request):
    """Representation selection only; Host + capability still authorize BOTH.

    Ordinary navigation opens the branded page. Native subscription requests
    receive unchanged client bytes, including clients that use */*. Never
    redirect the capability or fabricate device identity for compatibility.
    """
    fields = {}
    for name in ("accept", "user-agent", "sec-fetch-mode", "sec-fetch-dest"):
        values = request.headers.getlist(name)
        if (
            len(values) > 1
            or values
            and (len(values[0]) > 2048 or any(ord(c) < 32 for c in values[0]))
        ):
            raise ValueError("Некорректные заголовки запроса.")
        fields[name] = values[0].lower() if values else ""
    if (
        fields["sec-fetch-mode"] == "navigate"
        and fields["sec-fetch-dest"] == "document"
    ):
        return True
    agent = fields["user-agent"]
    if re.search(r"(?:^|[ /;(_-])(?:happ(?:plus|\s+plus)?|incy)(?:[ /;)_-]|$)", agent):
        return False
    qualities = {}
    for item in fields["accept"].split(","):
        media, *parameters = item.strip().split(";")
        if media not in {"text/html", "application/json", "text/plain"}:
            continue
        quality = 1.0
        for parameter in parameters:
            key, _, value = parameter.strip().partition("=")
            if key == "q":
                if not re.fullmatch(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", value):
                    raise ValueError("Некорректный Accept.")
                quality = float(value)
        qualities[media] = max(qualities.get(media, 0), quality)
    html = qualities.get("text/html", 0)
    return html > 0 and html > max(
        qualities.get("application/json", 0), qualities.get("text/plain", 0)
    )


def create_subscription_router(
    service, *, brand_asset_store=None, public_checkout=None
):
    router = APIRouter()

    def request_slug(request):
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or request.url.query:
            raise SubscriptionNotFound()
        return service.links.slug_from_host(hosts[0])

    def failure(status, message):
        return Response(
            message,
            status_code=status,
            media_type="text/plain",
            headers=security_headers(),
        )

    async def respond(request, token, *, config):
        try:
            if not service.enabled:
                raise SubscriptionNotFound()
            slug = request_slug(request)
            if config:
                outgoing = {}
                for key in CLIENT_HEADERS:
                    values = request.headers.getlist(key)
                    if len(values) > 1:
                        raise ValueError()
                    if values:
                        outgoing[key] = values[0]
                payload = await service.config(slug, token, outgoing)
                return Response(
                    payload.body,
                    status_code=payload.status,
                    media_type=payload.content_type,
                    headers={**security_headers(), **payload.headers},
                )
            context, state, quota = await service.page(slug, token)
            return render_subscription_page(
                context,
                service.links,
                state,
                quota,
                checkout=bool(public_checkout is not None and public_checkout.enabled),
            )
        except SubscriptionNotFound:
            return failure(
                404, "Подписка не найдена. Откройте личную ссылку из вашего бота."
            )
        except SubscriptionExpired:
            return failure(410, "Срок подписки истёк. Продлите её в боте.")
        except ValueError:
            return failure(400, "Не удалось обработать запрос приложения.")
        except Exception:
            return failure(
                503,
                "Не удалось обновить подписку. Повторите позже. Повторно платить не нужно.",
            )

    @router.get("/s/{token}")
    async def subscription_page(request: Request, token: str):
        return await respond(request, token, config=False)

    @router.get("/s/{token}/config")
    async def subscription_config(request: Request, token: str):
        return await respond(request, token, config=True)

    @router.get("/partner-subscription-assets/{filename}")
    async def asset(request: Request, filename: str):
        try:
            if not service.enabled:
                raise SubscriptionNotFound()
            # Assets contain no capabilities or user data. Still restrict Host.
            hosts = request.headers.getlist("host")
            if len(hosts) != 1:
                raise SubscriptionNotFound()
            service.links.slug_from_host(hosts[0])
            if filename not in {"subscription.css", "subscription.js"}:
                raise SubscriptionNotFound()
            return FileResponse(ASSETS / filename, headers=security_headers())
        except SubscriptionNotFound:
            return failure(404, "Не найдено.")

    @router.get("/partner-subscription-assets/logo/{digest}.png")
    async def logo(request: Request, digest: str):
        try:
            if not service.enabled or brand_asset_store is None:
                raise SubscriptionNotFound()
            slug = request_slug(request)
            png = await brand_asset_store.public_logo(slug, digest)
            return Response(png, media_type="image/png", headers=security_headers())
        except Exception:
            return failure(404, "Не найдено.")

    @router.get("/{token}/config")
    async def short_config(request: Request, token: str):
        if not service.links.settings.short_links:
            return failure(404, "Не найдено.")
        return await respond(request, token, config=True)

    @router.get("/{token}")
    async def short_subscription(request: Request, token: str):
        if not service.links.settings.short_links:
            return failure(404, "Не найдено.")
        try:
            config = not wants_html(request)
        except ValueError:
            return failure(400, "Не удалось обработать запрос приложения.")
        response = await respond(request, token, config=config)
        response.headers["Vary"] = "Accept, User-Agent, Sec-Fetch-Mode, Sec-Fetch-Dest"
        return response

    return router
