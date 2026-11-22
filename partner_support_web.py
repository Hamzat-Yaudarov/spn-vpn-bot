"""Narrow support surface signed ONLY by the central manager bot."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from partner_web import STATIC, _body, _plain, _uuid, _version
from services.partner_catalog import parse_price_kopecks
from services.partner_store import InvalidProject, ProjectConflict, ProjectNotFound
from services.partner_web_auth import PrivateAPIRoute, create_authenticate


def create_support_router(store, settings, *, operator_ids=()):
    # Explicit empty denies everybody. Public enrollment never broadens support.
    authenticate = create_authenticate(settings, manager_allowed_user_ids=operator_ids)
    router = APIRouter(route_class=PrivateAPIRoute)
    base = "/partners/support"

    async def actor(request):
        who = await authenticate(request)
        store.require_operator(who)
        return who

    async def call(operation):
        try:
            return _plain(await operation)
        except ProjectNotFound:
            raise HTTPException(404, "Проект или действие недоступны.") from None
        except ProjectConflict:
            raise HTTPException(409, "Настройки изменились. Обновите страницу.") from None
        except InvalidProject:
            raise HTTPException(400, "Проверьте название, цену, ссылки и оформление.") from None
        except Exception:
            raise HTTPException(503, "Не удалось загрузить или сохранить проект. Повторите позже.") from None

    @router.get(base, include_in_schema=False)
    @router.get(base + "/", include_in_schema=False)
    async def page():
        if not settings.enabled:
            raise HTTPException(404, "Раздел недоступен.")
        return FileResponse(STATIC / "support.html", headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
                "connect-src 'self'; img-src 'self' https: data:; object-src 'none'; "
                "base-uri 'none'; form-action 'self'; "
                "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
            ),
        })

    @router.get(base + "/api/settings")
    async def config(request: Request):
        return {"actor_id": await actor(request), "context": "platform_support"}

    @router.get(base + "/api/projects")
    async def projects(request: Request, cursor: int = 0):
        return await call(store.list_projects(await actor(request), cursor=cursor))

    @router.get(base + "/api/projects/{project_id}")
    async def project(project_id: str, request: Request):
        who = await actor(request)
        return await call(store.get_project(_uuid(project_id), who))

    @router.patch(base + "/api/projects/{project_id}")
    async def edit(project_id: str, request: Request):
        who = await actor(request)
        body = await _body(request, allowed={"expected_version", "title", "price", "support_url", "news_url", "brand"},
                           required={"expected_version"})
        version = _version(body.pop("expected_version"))
        if not body:
            raise HTTPException(400, "Нет изменений для сохранения.")
        if "price" in body:
            try:
                body["price_kopecks"] = parse_price_kopecks(body.pop("price"))
            except ValueError:
                raise HTTPException(400, "Цена подписки должна быть не ниже 199 ₽.") from None
        return await call(store.update_project(_uuid(project_id), who, version, body))

    return router
