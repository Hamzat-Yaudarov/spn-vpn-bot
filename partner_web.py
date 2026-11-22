"""Feature-gated partner API; external entry ports are opt-in and injected.

The existing SPN application injects NO fee provider/launcher. No migration,
merchant credential discovery, background launch or production bot write occurs
on import/startup. Entry mutations require trusted manager auth and owner scope.
"""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from services.partner_catalog import catalog, parse_price_kopecks, project_hostname
from services.partner_security import (
    BotConnectionError,
    PartnerConfigurationError,
    PartnerSettings,
    TelegramBotVerifier,
    TokenVault,
    validate_bot_token,
)
from services.partner_web_auth import create_authenticate, create_tenant_authenticate, PrivateAPIRoute


STATIC = Path(__file__).parent / "static" / "partners"
MAX_BODY = 16_384


def _plain(value):
    if isinstance(value, dict):
        return {
            key: _plain(item)
            for key, item in value.items()
            if key not in {"encrypted_token", "token", "token_ciphertext"}
        }
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, (UUID, date, datetime)):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    return value


def _uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Некорректный идентификатор.") from None


def _version(value):
    if type(value) is not int or not 1 <= value <= 2_147_483_646:
        raise HTTPException(400, "Обновите проект перед сохранением.")
    return value


async def _body(request, *, allowed, required=()):
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

        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError()
                result[key] = item
            return result

        value = json.loads(content, object_pairs_hook=unique)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Некорректный JSON.") from None
    if (
        not isinstance(value, dict)
        or value.keys() - set(allowed)
        or set(required) - value.keys()
    ):
        raise HTTPException(400, "Некорректные поля запроса.")
    return value


async def _main_addons():
    # Explicit call-time adapter; configuration and working DB are not imported
    # by this module. Discounts are the same source as the main bot's checkout.
    import database as db
    from config import BYPASS_TRAFFIC_PACKAGES
    from services.discounts import calculate_discounted_price
    from services.partner_catalog import load_main_addon_catalog

    discounts = await db.get_active_discounts()
    adapter = load_main_addon_catalog()
    traffic = {
        code: {
            **package,
            "price": str(
                calculate_discounted_price(
                    package["price"],
                    discounts,
                    product_type="traffic",
                    code=code,
                    plan_kind="bypass",
                )["price"]
            ),
        }
        for code, package in BYPASS_TRAFFIC_PACKAGES.items()
    }
    return {"traffic_packages": traffic, "device_options": adapter["device_options"]}


def create_router(
    store,
    settings: PartnerSettings,
    *,
    verifier=None,
    addon_loader=None,
    commerce_store=None,
    enrollment_store=None,
    entry_checkout=None,
    managed_service=None,
    management_store=None,
    finance_store=None,
    brand_asset_store=None,
    operator_ids=(),
    customer_checkout=None,
    manager_allowed_user_ids=None,
    surface="legacy",
    route_base="/partners",
    tenant_project_loader=None,
):
    from services.partner_store import InvalidProject, ProjectConflict, ProjectNotFound

    if surface not in {"legacy", "enrollment", "workspace"}:
        raise ValueError("Unknown partner surface")
    router = APIRouter(route_class=PrivateAPIRoute)
    api = route_base + "/api"
    workspace = surface == "workspace"
    enrollment = surface == "enrollment"
    from services.partner_entry_pricing import EntryPricingPolicy, ENTRY_PILOT_PRICE_KOPECKS
    from services.partner_catalog import ENTRY_FEE_KOPECKS

    entry_pricing = getattr(enrollment_store, "entry_pricing", None)
    if not isinstance(entry_pricing, EntryPricingPolicy):
        entry_pricing = EntryPricingPolicy()
    pay_first_enabled = bool(
        enrollment and getattr(enrollment_store, "payment_first_enabled", False) is True
        and entry_pricing.public_sales_enabled
    )
    verifier = verifier or TelegramBotVerifier()
    addon_loader = addon_loader or _main_addons

    def enabled():
        if not settings.enabled:
            raise HTTPException(404, "Раздел недоступен.")
        try:
            return settings.check()
        except PartnerConfigurationError:
            raise HTTPException(
                503, "Партнёрский кабинет ещё не настроен оператором."
            ) from None

    owner = (
        create_tenant_authenticate(
            settings, project_loader=tenant_project_loader or store.get_workspace_auth_project,
            management_store=management_store,
            manager_allowed_user_ids=manager_allowed_user_ids,
        )
        if workspace else create_authenticate(
            settings, manager_allowed_user_ids=manager_allowed_user_ids
        )
    )

    async def project_owner(request):
        actor = await owner(request)
        if workspace and request.state.partner_workspace["project"].get("role") != "owner":
            raise HTTPException(403, "Недостаточно прав для настроек проекта.")
        return actor

    async def call(method, *args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except ProjectNotFound:
            raise HTTPException(404, "Проект не найден.") from None
        except ProjectConflict:
            raise HTTPException(
                409,
                "Проект изменился или это подключение уже используется. Обновите данные.",
            ) from None
        except InvalidProject:
            raise HTTPException(
                400, "Проверьте название, ссылки, цену и настройки оформления."
            ) from None
        except Exception:
            # No raw DB/SQL/network errors (possibly including secrets) in UI.
            raise HTTPException(
                503, "Хранилище проектов пока недоступно. Повторите позже."
            ) from None

    def public(project):
        result = _plain(project)
        if enrollment:
            fields = {"id", "owner_tg_id", "title", "slug", "price_kopecks", "version", "status", "bot", "created_at", "updated_at"}
            result = {key: value for key, value in result.items() if key in fields}
            result["workspace_available"] = project.get("status") == "active"
            return result
        result["hostname"] = project_hostname(result["slug"], settings.base_domain)
        result["stage"] = "staging" if customer_checkout else "setup"
        result["sales_enabled"] = bool(
            project.get("status") == "active"
            and customer_checkout
            and customer_checkout.provider is not None
            and customer_checkout.provisioner is not None
        )
        return result

    @router.get(route_base, include_in_schema=False)
    @router.get(route_base + "/", include_in_schema=False)
    async def page():
        enabled()
        return FileResponse(
            STATIC / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self' https://telegram.org; "
                    "style-src 'self'; connect-src 'self'; img-src 'self' https: data:; "
                    "object-src 'none'; base-uri 'none'; form-action 'self'; "
                    "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
                ),
            },
        )

    @router.get(api + "/settings")
    async def get_settings(request: Request):
        actor = await owner(request)
        if management_store is not None and not workspace:
            try:
                await management_store.record_manager_identity(actor)
            except Exception:
                raise HTTPException(
                    503, "Не удалось проверить профиль управляющего кабинета."
                ) from None
        return {
            "enabled": True,
            "stage": "staging" if customer_checkout else "setup",
            "sales_enabled": bool(
                customer_checkout
                and customer_checkout.provider is not None
                and customer_checkout.provisioner is not None
            ),
            "base_domain": enabled(),
            "billing_days": 30,
            "bot_connection_available": bool(settings.encryption_keys),
            "managed_bot_creation_enabled": bool(enrollment and managed_service is not None and managed_service.enabled is True),
            "managed_bot_creation_available": bool(enrollment and managed_service is not None and managed_service.available is True),
            "support_url": "https://t.me/vpnislam",
            "actor_id": actor,
            "entry_fee_kopecks": entry_pricing.price_for(actor),
            "owner_entry_pilot": entry_pricing.is_pilot_for(actor),
            "public_entry_trial": entry_pricing.public_trial_enabled,
            "public_entry_sales": entry_pricing.public_sales_enabled,
            "pay_first_enabled": pay_first_enabled,
            "surface": surface,
            "workspace_project_id": request.state.partner_workspace["project_id"] if workspace else None,
            "is_operator": not (workspace or enrollment) and finance_store is not None and actor in operator_ids,
            "features": {
                "management": not enrollment and management_store is not None,
                "finance": not enrollment and finance_store is not None,
                "branding": not enrollment and brand_asset_store is not None,
            },
        }

    @router.get(api + "/projects")
    async def list_projects(request: Request):
        actor = await owner(request)
        if workspace:
            return {"items": [public(request.state.partner_workspace["project"])]}
        projects = await call(store.list_enrollment_projects if pay_first_enabled else store.list_projects, actor)
        if management_store is not None and not enrollment:
            try:
                visible = await management_store.visible_projects(actor)
                known = {item["id"] for item in projects}
                projects.extend(
                    item for item in visible["items"] if item["id"] not in known
                )
            except Exception:
                raise HTTPException(
                    503, "Не удалось проверить доступ к проектам. Повторите позже."
                ) from None
        return {"items": [public(item) for item in projects]}

    @router.post(api + "/projects", status_code=201)
    async def create_project(request: Request):
        actor = await owner(request)
        if pay_first_enabled:
            # One server-owned enrollment draft per owner, under the shared
            # owner lock; the old manual-draft path must not race checkout.
            raise HTTPException(404, "Используйте кнопку оплаты подключения.")
        body = await _body(
            request,
            allowed={"request_key", "slug", "title", "price"},
            required={"request_key", "slug", "title"},
        )
        key = _uuid(body["request_key"])
        try:
            price = parse_price_kopecks(body.get("price", "199.00"))
        except ValueError:
            raise HTTPException(
                400, "Цена должна быть не ниже 199 ₽, с точностью до копеек."
            ) from None
        project = await call(
            store.create_project, actor, key, body["slug"], body["title"], price
        )
        return public(project)

    @router.get(api + "/projects/{project_id}")
    async def get_project(project_id: str, request: Request):
        actor = await owner(request)
        if workspace:
            return public(request.state.partner_workspace["project"])
        return public(await call(store.get_project, _uuid(project_id), actor))

    @router.patch(api + "/projects/{project_id}")
    async def update_project(project_id: str, request: Request):
        actor = await project_owner(request)
        body = await _body(
            request,
            allowed={
                "expected_version",
                "title",
                "price",
                "support_url",
                "news_url",
                "brand",
            },
            required={"expected_version"},
        )
        version = _version(body.pop("expected_version"))
        if not body:
            raise HTTPException(400, "Нет изменений для сохранения.")
        if "price" in body:
            try:
                body["price_kopecks"] = parse_price_kopecks(body.pop("price"))
            except ValueError:
                raise HTTPException(
                    400, "Цена должна быть не ниже 199 ₽, с точностью до копеек."
                ) from None
        return public(
            await call(store.update_project, _uuid(project_id), actor, version, body)
        )

    @router.post(api + "/projects/{project_id}/managed-bot")
    async def create_managed_bot(project_id: str, request: Request):
        actor = await project_owner(request)
        if not enrollment or managed_service is None or managed_service.enabled is not True:
            raise HTTPException(404, "Создание бота пока недоступно.")
        project_id = _uuid(project_id)
        body = await _body(request, allowed={"expected_version"}, required={"expected_version"})
        version = _version(body["expected_version"])
        project = await call(store.get_project, project_id, actor)
        if project.get("status") not in {"draft", "ready"}:
            raise HTTPException(403, "Настройки действующего проекта доступны в его боте.")
        if project["version"] != version:
            raise HTTPException(409, "Проект изменился. Обновите подключение.")
        try:
            result = await managed_service.request_creation(project_id, actor, version)
            return _plain({key: result[key] for key in ("creation_url", "status", "expires_at") if key in result})
        except HTTPException:
            raise
        except Exception as error:
            if type(error).__name__ in {"ManagedBotConflict", "ProjectConflict"}:
                raise HTTPException(409, "Сначала завершите текущее подключение бота или обновите проект.") from None
            if type(error).__name__ in {"ManagedBotDenied", "ProjectNotFound"}:
                raise HTTPException(403, "Это подключение недоступно.") from None
            raise HTTPException(503, "Telegram пока не подтвердил создание ботов. Повторная оплата не нужна.") from None

    @router.post(api + "/projects/{project_id}/bot")
    async def connect_bot(project_id: str, request: Request):
        actor = await project_owner(request)
        if enrollment and managed_service is not None and managed_service.enabled is True:
            raise HTTPException(403, "Создайте бота кнопкой Telegram — токен вводить не нужно.")
        project_id = _uuid(project_id)
        body = await _body(
            request,
            allowed={"expected_version", "token"},
            required={"expected_version", "token"},
        )
        version = _version(body["expected_version"])
        project = await call(store.get_project, project_id, actor)
        if enrollment and project.get("status") not in {"draft", "ready"}:
            raise HTTPException(403, "Настройки действующего проекта доступны в его боте.")
        if project["version"] != version:
            raise HTTPException(
                409, "Проект изменился. Обновите данные перед подключением."
            )
        try:
            token = validate_bot_token(body["token"])
            if token.split(":", 1)[0] == settings.manager_bot_token.split(":", 1)[0]:
                raise ValueError()
        except ValueError:
            raise HTTPException(
                400, "Укажите токен отдельного партнёрского бота."
            ) from None
        try:
            vault = TokenVault(settings.encryption_keys)
            bot = await verifier.verify(token)
            encrypted = vault.encrypt(project_id, bot["id"], token)
        except PartnerConfigurationError:
            raise HTTPException(
                503, "Защищённое хранение токенов ещё не настроено оператором."
            ) from None
        except (BotConnectionError, ValueError, TypeError, KeyError):
            raise HTTPException(
                400, "Не удалось проверить бота. Проверьте токен и повторите."
            ) from None
        except Exception:
            raise HTTPException(503, "Проверка Telegram временно недоступна.") from None
        result = await call(
            store.save_bot_connection,
            project_id,
            actor,
            version,
            bot["id"],
            bot["username"],
            encrypted,
        )
        return public(result)

    @router.get(api + "/catalog")
    async def get_catalog(request: Request):
        actor = await owner(request)
        if enrollment:
            return catalog(19_900)
        project_id = request.query_params.get("project_id")
        price = 19_900
        if project_id:
            project = request.state.partner_workspace["project"] if workspace else await call(store.get_project, _uuid(project_id), actor)
            price = project["price_kopecks"]
        try:
            addons = await addon_loader()
            return catalog(price, **addons)
        except Exception:
            raise HTTPException(
                503, "Не удалось загрузить действующие цены докупок. Повторите позже."
            ) from None

    async def commerce_read(request, project_id, method, **kwargs):
        from services.partner_commerce_store import CommerceNotFound, InvalidCommerce

        actor = await project_owner(request)
        project_id = _uuid(project_id)
        # Authorization precedes optional module/storage checks, and the
        # commerce store repeats the owner predicate in its own SQL boundary.
        await call(store.get_project, project_id, actor)
        if commerce_store is None:
            raise HTTPException(
                503, "Клиентский раздел ещё не подключён на этом стенде."
            )
        try:
            return await getattr(commerce_store, method)(project_id, actor, **kwargs)
        except CommerceNotFound:
            raise HTTPException(404, "Проект не найден.") from None
        except InvalidCommerce:
            raise HTTPException(400, "Некорректный запрос.") from None
        except Exception:
            raise HTTPException(
                503, "Не удалось загрузить данные проекта. Повторите позже."
            ) from None

    @router.get(api + "/projects/{project_id}/overview")
    async def project_overview(project_id: str, request: Request):
        result = await commerce_read(request, project_id, "overview")
        return {**_plain(result), "sales_enabled": False}

    @router.get(api + "/projects/{project_id}/customers")
    async def project_customers(project_id: str, request: Request):
        rows = await commerce_read(request, project_id, "list_customers", limit=101)
        fields = {
            "tg_id",
            "created_at",
            "referrer_tg_id",
            "subscription_status",
            "expires_at",
        }
        items = []
        for row in rows[:100]:
            item = {k: v for k, v in row.items() if k in fields}
            # An unpaid reserved slot is not a purchased subscription. Keep the
            # public administrative state contract stable and avoid claiming it.
            if item.get("subscription_status") == "unpaid":
                item["subscription_status"] = "none"
            items.append(_plain(item))
        return {"items": items, "truncated": len(rows) > 100}

    @router.get(api + "/projects/{project_id}/orders")
    async def project_orders(project_id: str, request: Request):
        rows = await commerce_read(request, project_id, "list_orders", limit=101)
        fields = {
            "id",
            "tg_id",
            "price_kopecks",
            "status",
            "created_at",
            "paid_at",
            "purpose",
            "activation_issue",
        }
        return {
            "items": [
                _plain({k: v for k, v in row.items() if k in fields})
                for row in rows[:100]
            ],
            "truncated": len(rows) > 100,
        }

    async def entry_scope(request, project_id):
        actor = await owner(request)
        project_id = _uuid(project_id)
        project = await call(store.get_project, project_id, actor)
        if enrollment_store is None:
            raise HTTPException(
                503, "Подключение партнёрства ещё не настроено на этом стенде."
            )
        return project_id, actor, project

    async def entry_call(method, *args, **kwargs):
        from services.partner_checkout import (
            BillingUnavailable,
            CheckoutError,
            InvoiceRecoveryRequired,
            PaymentMismatch,
        )
        from services.partner_entry_checkout import EntryNotPaid
        from services.partner_enrollment_store import (
            EnrollmentConflict,
            EnrollmentNotFound,
            InvalidEnrollment,
        )

        try:
            return await method(*args, **kwargs)
        except EnrollmentNotFound:
            raise HTTPException(404, "Заказ вступления не найден.") from None
        except (
            EnrollmentConflict,
            InvoiceRecoveryRequired,
            PaymentMismatch,
            EntryNotPaid,
        ):
            raise HTTPException(
                409,
                "Заказ требует проверки. Обновите статус; повторно платить не нужно.",
            ) from None
        except InvalidEnrollment:
            raise HTTPException(
                400, "Проверьте подключение бота и условия вступления."
            ) from None
        except BillingUnavailable:
            raise HTTPException(
                503,
                "Обработка вступления пока недоступна. Если уже оплатили, повторять оплату не нужно.",
            ) from None
        except CheckoutError:
            raise HTTPException(
                503, "Не удалось проверить этот заказ. Повторная оплата не нужна."
            ) from None
        except Exception:
            raise HTTPException(
                503, "Подключение временно недоступно. Сохранённый заказ не потерян."
            ) from None

    def entry_envelope(order, result=None, *, project=None, actor):
        from services.partner_entry_checkout import public_entry_order

        eligible = project is None or project.get("status") in {"draft", "ready"}
        response = {
            "order": public_entry_order(order),
            "entry_fee_kopecks": entry_pricing.price_for(actor),
            "owner_entry_pilot": entry_pricing.is_pilot_for(actor),
            "public_entry_trial": entry_pricing.public_trial_enabled,
            "public_entry_sales": entry_pricing.public_sales_enabled,
            "pay_first_enabled": pay_first_enabled,
            "payments_available": bool(
                eligible
                and entry_checkout is not None
                and entry_checkout.payments_available
            ),
            "launch_available": bool(
                eligible
                and entry_checkout is not None
                and entry_checkout.launch_available
            ),
        }
        if response["order"] is not None and not response["payments_available"]:
            # Keep the saved invoice reconcilable, but do not invite a new fee
            # payment while the project/runtime is paused or unconfigured.
            response["order"].pop("confirmation_url", None)
        if order and order.get("needs_webhook_replace"):
            response["launch_state"] = "needs_webhook_replace"
        elif (order and order.get("status") == "paid_pending_launch"
                and "bot_id" in order and order["bot_id"] is None):
            response["launch_state"] = "awaiting_bot"
            response["creation_available"] = True
        if result:
            for key in ("launch_state", "message"):
                if key in result:
                    response[key] = result[key]
        return _plain(response)

    @router.post(api + "/enrollment/checkout")
    async def begin_enrollment_checkout(request: Request):
        # Only a verified central-manager identity may buy enrollment. Tenant
        # bot secrets, supplied owner IDs and anonymous requests are not proof.
        actor = await owner(request)
        if not pay_first_enabled:
            raise HTTPException(404, "Подключение пока недоступно.")
        body = await _body(request, allowed={"request_key", "expected_fee_kopecks"},
                           required={"request_key", "expected_fee_kopecks"})
        key = _uuid(body["request_key"])
        if (type(body["expected_fee_kopecks"]) is not int
                or body["expected_fee_kopecks"] != entry_pricing.price_for(actor)):
            raise HTTPException(409, "Стоимость изменилась. Обновите страницу.")
        if (entry_checkout is None or not entry_checkout.payments_available
                or managed_service is None or managed_service.available is not True):
            raise HTTPException(503, "Оплата временно недоступна. Попробуйте позже.")
        project = await call(store.prepare_enrollment_project, actor, key)
        project_id = project["id"]
        order = await entry_call(enrollment_store.get_current_entry_order, project_id, actor)
        if project.get("status") not in {"draft", "ready", "active"}:
            raise HTTPException(403, "Подключение требует помощи поддержки.")
        if not order or order.get("status") == "canceled":
            if project.get("status") == "active":
                raise HTTPException(409, "Ваш сервис уже создан. Откройте его бота.")
            from services.partner_enrollment_store import EnrollmentConflict
            try:
                order = await enrollment_store.create_entry_order(
                    project_id, actor, key, body["expected_fee_kopecks"]
                )
            except EnrollmentConflict:
                # Another tab can win creation with a different request key.
                # Recover that exact owner's project order, never a new fee.
                order = await entry_call(enrollment_store.get_current_entry_order, project_id, actor)
                if not order or order.get("status") == "canceled":
                    raise HTTPException(409, "Обновите состояние подключения.") from None
            except Exception:
                raise HTTPException(503, "Не удалось подготовить оплату. Повторите попытку.") from None
        result = None
        if order.get("status") == "created":
            result = await entry_call(entry_checkout.create_checkout, project_id, actor, str(order["id"]))
            order = await entry_call(enrollment_store.get_entry_order, project_id, actor, str(order["id"]))
        return {**entry_envelope(order, result, project=project, actor=actor), "project": public(project)}

    @router.get(api + "/projects/{project_id}/entry")
    async def get_entry(project_id: str, request: Request):
        project_id, actor, project = await entry_scope(request, project_id)
        order = await entry_call(
            enrollment_store.get_current_entry_order, project_id, actor
        )
        return entry_envelope(order, project=project, actor=actor)

    @router.post(api + "/projects/{project_id}/entry")
    async def create_entry(project_id: str, request: Request):
        project_id, actor, project = await entry_scope(request, project_id)
        body = await _body(
            request,
            allowed={"request_key", "expected_fee_kopecks"},
            required={"request_key", "expected_fee_kopecks"},
        )
        if (
            type(body["expected_fee_kopecks"]) is not int
            or body["expected_fee_kopecks"] not in {ENTRY_FEE_KOPECKS, ENTRY_PILOT_PRICE_KOPECKS}
        ):
            raise HTTPException(409, "Обновите и подтвердите актуальную стоимость вступления.")
        key = _uuid(body["request_key"])
        if entry_checkout is None or not entry_checkout.payments_available:
            raise HTTPException(
                503, "Вступление пока не подключено. Заказ и списание не создавались."
            )
        order = await entry_call(
            enrollment_store.create_entry_order,
            project_id,
            actor,
            key,
            body["expected_fee_kopecks"],
        )
        return entry_envelope(order, project=project, actor=actor)

    async def entry_operation(project_id, order_id, request, operation):
        project_id, actor, project = await entry_scope(request, project_id)
        order_id = _uuid(order_id)
        body = await _body(
            request, allowed={"replace_existing"} if operation == "launch" else set()
        )
        if (
            operation == "launch"
            and type(body.get("replace_existing", False)) is not bool
        ):
            raise HTTPException(400, "Замена подключения требует явного подтверждения.")
        # Scope-check before privileged provider/Telegram work, even if the
        # installation is disabled or the supplied order UUID belongs elsewhere.
        await entry_call(enrollment_store.get_entry_order, project_id, actor, order_id)
        if entry_checkout is None:
            raise HTTPException(503, "Обработка вступления пока не подключена.")
        method = {
            "checkout": entry_checkout.create_checkout,
            "check": entry_checkout.reconcile,
            "launch": entry_checkout.launch,
        }[operation]
        kwargs = (
            {"replace_existing": body.get("replace_existing", False)}
            if operation == "launch"
            else {}
        )
        result = await entry_call(method, project_id, actor, order_id, **kwargs)
        current = await entry_call(
            enrollment_store.get_entry_order, project_id, actor, order_id
        )
        return entry_envelope(current, result, project=project, actor=actor)

    @router.post(api + "/projects/{project_id}/entry/{order_id}/checkout")
    async def checkout_entry(project_id: str, order_id: str, request: Request):
        return await entry_operation(project_id, order_id, request, "checkout")

    @router.post(api + "/projects/{project_id}/entry/{order_id}/check")
    async def check_entry(project_id: str, order_id: str, request: Request):
        return await entry_operation(project_id, order_id, request, "check")

    @router.post(api + "/projects/{project_id}/entry/{order_id}/launch")
    async def launch_entry(project_id: str, order_id: str, request: Request):
        return await entry_operation(project_id, order_id, request, "launch")

    # Filter only our concrete routes. New FastAPI versions defer included
    # routers, whose wrappers intentionally have no path/methods attributes.
    # Extensions already apply their own surface and tenant-scope guards.
    if enrollment:
        allowed = {
            (api + "/settings", "GET"), (api + "/catalog", "GET"),
            (api + "/projects", "GET"), (api + "/projects", "POST"),
            (api + "/projects/{project_id}", "GET"),
            (api + "/projects/{project_id}/bot", "POST"),
            (api + "/projects/{project_id}/managed-bot", "POST"),
            (api + "/enrollment/checkout", "POST"),
        }
        router.routes = [route for route in router.routes if
            route.path in {route_base, route_base + "/"}
            or route.path.startswith(api + "/projects/{project_id}/entry")
            or any((route.path, method) in allowed for method in route.methods)]
    elif workspace:
        router.routes = [route for route in router.routes if
            "/entry" not in route.path
            and "/enrollment/" not in route.path
            and not route.path.endswith("/managed-bot")
            and not (route.path == api + "/projects" and "POST" in route.methods)]

    if management_store is not None and not enrollment:
        from partner_management_web import create_management_router

        router.include_router(
            create_management_router(
                management_store, authenticate=owner, enabled=settings.enabled,
                api_base=api, workspace_scope=workspace,
            )
        )
    if finance_store is not None and not enrollment:
        from partner_finance_web import create_router as finance_router

        router.include_router(
            finance_router(finance_store, authenticate=project_owner, enabled=settings.enabled,
                api_base=api, allow_operator=not workspace)
        )
    if brand_asset_store is not None and not enrollment:
        from partner_brand_web import create_router as create_brand_router

        router.include_router(
            create_brand_router(
                brand_asset_store, authenticate=project_owner, enabled=settings.enabled, api_base=api,
            )
        )
    return router
