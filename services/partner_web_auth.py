"""Separate central enrollment identity and strictly project-bound tenant proof."""

from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse

from services.partner_security import PartnerConfigurationError, TokenVault
from services.partner_manager_access import normalize_manager_allowed_user_ids
from services.telegram_auth import TelegramAuthError, validate_telegram_init_data


class PrivateAPIRoute(APIRoute):
    """Never let proxies/browser caches retain private finance/client responses."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                response = await original(request)
            except HTTPException as error:
                response = JSONResponse(
                    {"detail": error.detail},
                    status_code=error.status_code,
                    headers=error.headers,
                )
            except RequestValidationError:
                # Validation errors can contain submitted secrets. Neither echo
                # them nor let a cache retain an authenticated error response.
                response = JSONResponse(
                    {"detail": "Некорректный запрос."}, status_code=422
                )
            if "no-store" not in response.headers.get("Cache-Control", ""):
                response.headers["Cache-Control"] = "private, no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        return handler


def create_authenticate(settings, *, manager_allowed_user_ids=None):
    allowed_user_ids = normalize_manager_allowed_user_ids(manager_allowed_user_ids)

    async def authenticate(request):
        request.state.partner_workspace = None
        if not settings.enabled:
            raise HTTPException(404, "Раздел недоступен.")
        try:
            settings.check()
        except PartnerConfigurationError:
            raise HTTPException(503, "Кабинет ещё не настроен оператором.") from None
        values = request.headers.getlist("authorization")
        if (
            len(values) != 1
            or not values[0].startswith("tma ")
            or len(values[0]) > 16384
        ):
            raise HTTPException(
                401, "Откройте кабинет через управляющего бота платформы."
            )
        try:
            data = values[0][4:]
            pairs = parse_qsl(data, keep_blank_values=True, max_num_fields=32)
            if not pairs or len(pairs) != len({key for key, _ in pairs}):
                raise TelegramAuthError()
            user = validate_telegram_init_data(
                data, settings.manager_bot_token, max_age_seconds=3600
            )
            actor = user.get("id")
            if type(actor) is not int or not 1 <= actor <= 2**63 - 1:
                raise TelegramAuthError()
        except (ValueError, TypeError, TelegramAuthError):
            raise HTTPException(
                401, "Авторизация устарела. Откройте кабинет заново."
            ) from None
        if allowed_user_ids is not None and actor not in allowed_user_ids:
            raise HTTPException(403, "Раздел недоступен.")
        return actor

    return authenticate


def create_tenant_authenticate(
    settings, *, project_loader, management_store=None, manager_allowed_user_ids=None
):
    """A tenant token proves ONLY its server-resolved project, never global identity.

    Tenant owners possess their bot token and can synthesize HMAC initData. The
    immutable project boundary, owner/member checks and absence of global APIs
    are therefore required even after Telegram's signature has been verified.
    """
    if not callable(project_loader):
        raise TypeError("A private tenant credential loader is required")
    allowed_user_ids = normalize_manager_allowed_user_ids(manager_allowed_user_ids)

    async def authenticate(request):
        request.state.partner_workspace = None
        if not settings.enabled:
            raise HTTPException(404, "Раздел недоступен.")
        try:
            settings.check()
        except PartnerConfigurationError:
            raise HTTPException(503, "Личный кабинет ещё не настроен.") from None
        try:
            project_id = str(UUID(request.path_params.get("workspace_project_id", "")))
            requested = request.path_params.get("project_id")
            query_ids = request.query_params.getlist("project_id")
            if requested is not None and str(UUID(requested)) != project_id:
                raise ValueError()
            if len(query_ids) > 1 or query_ids and str(UUID(query_ids[0])) != project_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(403, "Проект или действие недоступны.") from None
        values = request.headers.getlist("authorization")
        if len(values) != 1 or not values[0].startswith("tma ") or len(values[0]) > 16384:
            raise HTTPException(401, "Откройте личный кабинет в боте проекта.")
        try:
            project = await project_loader(project_id)
            bot = project["bot"]
            owner_id, bot_id = project["owner_tg_id"], bot["id"]
            if (
                str(UUID(str(project["id"]))) != project_id
                or project["status"] != "active"
                or type(owner_id) is not int or not 0 < owner_id < 2**63
                or type(bot_id) is not int or not 0 < bot_id < 2**63
                or bot_id == int(settings.manager_bot_token.split(":", 1)[0])
            ):
                raise ValueError()
            token = TokenVault(settings.encryption_keys).decrypt(
                project_id, bot_id, project["encrypted_token"]
            )
        except Exception:
            raise HTTPException(503, "Не удалось проверить подключение проекта.") from None
        try:
            data = values[0][4:]
            pairs = parse_qsl(data, keep_blank_values=True, max_num_fields=32)
            if not pairs or len(pairs) != len({key for key, _ in pairs}):
                raise TelegramAuthError()
            user = validate_telegram_init_data(data, token, max_age_seconds=3600)
            actor = user.get("id")
            if type(actor) is not int or not 0 < actor < 2**63:
                raise TelegramAuthError()
        except (ValueError, TypeError, TelegramAuthError):
            raise HTTPException(401, "Авторизация устарела. Откройте личный кабинет заново.") from None
        if allowed_user_ids is not None and (
            owner_id not in allowed_user_ids or actor not in allowed_user_ids
        ):
            raise HTTPException(403, "Раздел недоступен.")
        if actor == owner_id:
            role = "owner"
        else:
            if management_store is None:
                raise HTTPException(403, "Проект или действие недоступны.")
            try:
                access = await management_store.authorize(project_id, actor, "referrals")
                if (
                    str(UUID(str(access["project_id"]))) != project_id
                    or access["role"] not in {"viewer", "support", "marketer"}
                ):
                    raise ValueError()
                role = access["role"]
            except Exception as error:
                if type(error).__name__ in {"ManagementDenied", "ValueError", "KeyError", "TypeError"}:
                    raise HTTPException(403, "Проект или действие недоступны.") from None
                raise HTTPException(503, "Не удалось проверить права проекта.") from None
        from services.partner_management import PERMISSIONS
        permissions = PERMISSIONS[role]
        # The loader is private and may gain further credential columns later.
        # Only the registry's public fields belong in the request/UI context.
        public_fields = {
            "id", "owner_tg_id", "title", "slug", "price_kopecks", "version",
            "status", "support_url", "news_url", "brand", "bot",
            "created_at", "updated_at",
        }
        public_project = {key: value for key, value in project.items() if key in public_fields}
        public_project.update(role=role, permissions=sorted(permissions))
        request.state.partner_workspace = {
            "project_id": project_id, "actor_id": actor, "project": public_project,
        }
        return actor

    return authenticate
