"""Opt-in Telegram Managed Bots onboarding; no I/O on import/construction.

Official sources: core.telegram.org/bots/features#managed-bots and Bot API
ManagedBotUpdated/getManagedBotToken. Deep links carry no project context;
the verified Telegram user and one durable pending draft supply the binding.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import re
from urllib.parse import quote
from uuid import UUID

import asyncpg

from services.partner_manager_access import normalize_manager_allowed_user_ids
from services.partner_security import validate_bot_token
from services.partner_store import ProjectConflict, ProjectNotFound, _public, _PROJECT_COLUMNS


class ManagedBotUnavailable(RuntimeError):
    pass


class ManagedBotConflict(ProjectConflict):
    pass


def positive(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ManagedBotConflict("Некорректный запрос создания бота.")
    return value


def project_uuid(value):
    try: return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ManagedBotConflict("Проект не найден.") from None


def bot_identity(bot, expected=None):
    if (not isinstance(bot, dict) or bot.get("is_bot") is not True
            or type(bot.get("id")) is not int or not 0 < bot["id"] < 2**63
            or expected is not None and bot["id"] != expected
            or not isinstance(bot.get("username"), str)
            or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot["username"])):
        raise ManagedBotConflict("Telegram пока не подтвердил нового бота.")
    return bot


def initial_name(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 64
            or any(ord(c) < 32 or ord(c) == 127 or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for c in value)):
        return None
    return value.strip()


def already_processed(request, update_id, now):
    last = request["last_update_id"]
    if last is None or last < update_id:
        return False
    # Bot API update_id may restart at a random value after a quiet week.
    # A late refresh still fetches the CURRENT manager-authorized token; it
    # cannot create a project, change ownership, issue money or replace a bot.
    recent = request["bound_at"]
    return recent is None or now - recent < timedelta(days=7)


def event_identity(update):
    """Only call after the manager webhook's secret/Host validation."""
    try:
        if not isinstance(update, dict): return None
        update_id = update["update_id"]
        if type(update_id) is not int or not 0 <= update_id < 2**63: return None
        if "managed_bot" in update:
            event = update["managed_bot"]
            user, bot = event["user"], event["bot"]
        else:
            message = update["message"]
            if (message.get("chat", {}).get("type") != "private"
                    or any(key in message for key in ("forward_origin", "forward_from", "sender_chat", "via_bot", "business_connection_id"))):
                return None
            user, bot = message["from"], message["managed_bot_created"]["bot"]
            if message["chat"]["id"] != user["id"]: return None
        if not isinstance(user, dict) or user.get("is_bot") is not False: return None
        owner_id = positive(user["id"])
        bot = bot_identity(bot)
        return owner_id, bot["id"], update_id
    except (KeyError, TypeError, AttributeError, ManagedBotConflict):
        return None


class TelegramManagedBotAdapter:
    async def _call(self, token, method, body=None):
        try:
            validate_bot_token(token)
            if method not in {"getMe", "getManagedBotToken"}: raise ValueError()
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20, connect=5), trust_env=False) as session:
                async with session.post("https://api.telegram.org/bot" + token + "/" + method,
                                        json=body or {}, allow_redirects=False) as response:
                    if response.status != 200: raise ValueError()
                    raw = bytearray()
                    async for part in response.content.iter_chunked(8192):
                        raw.extend(part)
                        if len(raw) > 65_536: raise ValueError()
                    result = json.loads(raw)
                    if result.get("ok") is not True: raise ValueError()
                    return result["result"]
        except Exception:
            raise ManagedBotUnavailable("Telegram пока не подтвердил создание бота. Повторная оплата не нужна.") from None

    async def get_me(self, token):
        return bot_identity(await self._call(token, "getMe"), int(validate_bot_token(token).split(":", 1)[0]))

    async def get_managed_bot_token(self, manager_token, bot_id):
        positive(bot_id)
        token = await self._call(manager_token, "getManagedBotToken", {"user_id": bot_id})
        try:
            validate_bot_token(token)
            if int(token.split(":", 1)[0]) != bot_id: raise ValueError()
            return token
        except Exception:
            raise ManagedBotUnavailable("Telegram пока не подтвердил подключение бота.") from None


class ManagedBotStore:
    def __init__(self, pool_getter, *, payment_first_enabled=False):
        if not callable(pool_getter): raise TypeError("pool_getter must be callable")
        if type(payment_first_enabled) is not bool: raise TypeError("payment_first_enabled must be bool")
        self._pool_getter = pool_getter
        self.payment_first_enabled = payment_first_enabled

    async def pool(self):
        value = self._pool_getter()
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    async def lock(conn, owner_id):
        key = int.from_bytes(hashlib.sha256(f"wl-managed-owner:{owner_id}".encode()).digest()[:8], "big", signed=True)
        await conn.execute("SELECT pg_advisory_xact_lock($1)", key)

    async def _binding_order(self, conn, project_id, owner_id):
        """Lock the paid entitlement, never trust UI/payment-return parameters.

        Legacy (bot-first) installs still reject any saved entry order. The
        opt-in pay-first path can bind exactly one bot only after canonical
        payment has been committed by the entry verifier.
        """
        order = await conn.fetchrow("""SELECT * FROM wl_entry_orders WHERE project_id=$1
            AND owner_tg_id=$2 AND status<>'canceled' FOR UPDATE""", project_id, owner_id)
        if self.payment_first_enabled:
            if (not order or order["status"] != "paid_pending_launch"
                    or order["bot_id"] is not None or order["paid_at"] is None
                    or not order["provider_payment_id"]):
                raise ManagedBotConflict("Сначала оплатите создание сервиса.")
        elif order:
            raise ManagedBotConflict("Сначала восстановите сохранённое вступление.")
        return order

    async def begin_request(self, project_id, owner_id, expected_version, *, now, expires_at):
        project_id, owner_id = project_uuid(project_id), positive(owner_id)
        positive(expected_version)
        pool = await self.pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self.lock(conn, owner_id)
                    project = await conn.fetchrow("SELECT * FROM wl_projects WHERE id=$1 AND owner_tg_id=$2 FOR UPDATE", project_id, owner_id)
                    if not project: raise ProjectNotFound("Проект не найден.")
                    if project["version"] != expected_version: raise ManagedBotConflict("Обновите проект перед созданием бота.")
                    if project["status"] not in {"draft", "ready"} or project["bot_id"] is not None:
                        raise ManagedBotConflict("Бот этого проекта уже подключён. Перепривязка не выполняется.")
                    await self._binding_order(conn, project_id, owner_id)
                    await conn.execute("""UPDATE wl_managed_bot_requests SET status='expired'
                        WHERE owner_tg_id=$1 AND status='pending' AND bot_id IS NULL AND expires_at<=$2""", owner_id, now)
                    pending = await conn.fetchrow("SELECT * FROM wl_managed_bot_requests WHERE owner_tg_id=$1 AND status='pending' FOR UPDATE", owner_id)
                    if pending:
                        if str(pending["project_id"]) != str(project_id): raise ManagedBotConflict("Сначала завершите создание бота для другого проекта.")
                        if pending["expires_at"] <= now: raise ManagedBotConflict("Подключение уже начато. Восстановите его, не создавая другого бота.")
                        return {"project_id": str(project_id), "title": project["title"], "expires_at": pending["expires_at"], "status": "pending"}
                    prior = await conn.fetchrow("SELECT * FROM wl_managed_bot_requests WHERE project_id=$1 FOR UPDATE", project_id)
                    if prior:
                        if prior["status"] != "expired" or prior["bot_id"] is not None:
                            raise ManagedBotConflict("Подключение этого проекта требует проверки.")
                        await conn.execute("""UPDATE wl_managed_bot_requests SET status='pending',project_version=$2,
                            created_at=$3,expires_at=$4 WHERE project_id=$1""", project_id, expected_version, now, expires_at)
                    else:
                        await conn.execute("""INSERT INTO wl_managed_bot_requests(project_id,owner_tg_id,project_version,created_at,expires_at)
                            VALUES($1,$2,$3,$4,$5)""", project_id, owner_id, expected_version, now, expires_at)
                    return {"project_id": str(project_id), "title": project["title"], "expires_at": expires_at, "status": "pending"}
        except asyncpg.UniqueViolationError:
            raise ManagedBotConflict("Создание бота уже начато. Обновите текущий проект.") from None

    async def claim_event(self, owner_id, bot_id, update_id, *, now):
        owner_id, bot_id = positive(owner_id), positive(bot_id)
        pool = await self.pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self.lock(conn, owner_id)
                    request = await conn.fetchrow("SELECT * FROM wl_managed_bot_requests WHERE bot_id=$1 FOR UPDATE", bot_id)
                    if request is not None and request["owner_tg_id"] != owner_id: return None
                    if request is None:
                        request = await conn.fetchrow("SELECT * FROM wl_managed_bot_requests WHERE owner_tg_id=$1 AND status='pending' FOR UPDATE", owner_id)
                    if request is None or request["status"] not in {"pending", "bound"}: return None
                    if request["bot_id"] not in (None, bot_id): return None
                    if already_processed(request, update_id, now): return None
                    project = await conn.fetchrow("SELECT * FROM wl_projects WHERE id=$1 AND owner_tg_id=$2 FOR UPDATE", request["project_id"], owner_id)
                    if not project or project["status"] not in {"draft", "ready", "active"}: return None
                    if request["status"] == "bound":
                        if project["bot_id"] != bot_id: return None
                    else:
                        if ((request["expires_at"] <= now and request["bot_id"] is None)
                                or project["bot_id"] is not None or project["status"] == "active"): return None
                        if await conn.fetchval("SELECT 1 FROM wl_projects WHERE bot_id=$1", bot_id): return None
                        try:
                            await self._binding_order(conn, request["project_id"], owner_id)
                        except ManagedBotConflict:
                            return None
                        await conn.execute("UPDATE wl_managed_bot_requests SET bot_id=$2 WHERE project_id=$1", request["project_id"], bot_id)
                    return {"project_id": str(request["project_id"]), "owner_tg_id": owner_id, "bot_id": bot_id,
                            "update_id": update_id, "project_version": project["version"],
                            "initial_version": request["project_version"], "rotation": request["status"] == "bound"}
        except asyncpg.UniqueViolationError:
            return None

    async def complete_binding(self, job, username, encrypted_token, *, first_name, now):
        pid, owner = project_uuid(job["project_id"]), positive(job["owner_tg_id"])
        bot_id, update_id = positive(job["bot_id"]), job["update_id"]
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) or not 32 <= len(encrypted_token) <= 16384:
            raise ManagedBotConflict("Не удалось сохранить подключение бота.")
        pool = await self.pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self.lock(conn, owner)
                request = await conn.fetchrow("SELECT * FROM wl_managed_bot_requests WHERE project_id=$1 AND owner_tg_id=$2 FOR UPDATE", pid, owner)
                project = await conn.fetchrow("SELECT * FROM wl_projects WHERE id=$1 AND owner_tg_id=$2 FOR UPDATE", pid, owner)
                if not request or not project or request["bot_id"] != bot_id or request["status"] not in {"pending", "bound"}:
                    raise ManagedBotConflict("Подключение проекта уже изменилось.")
                if already_processed(request, update_id, now):
                    return _public(project)
                if project["status"] not in {"draft", "ready", "active"}:
                    raise ManagedBotConflict("Проект временно недоступен.")
                if request["status"] == "bound":
                    if project["bot_id"] != bot_id: raise ManagedBotConflict("Нельзя заменить бота действующего проекта.")
                else:
                    if project["bot_id"] is not None or project["status"] == "active":
                        raise ManagedBotConflict("Сначала обновите состояние подключения.")
                    order = await self._binding_order(conn, pid, owner)
                title = project["title"]
                adopted = initial_name(first_name)
                if request["status"] == "pending" and title in {"Мой VPN", "Мой сервис"} and project["version"] == request["project_version"] and adopted:
                    title = adopted
                row = await conn.fetchrow(f"""UPDATE wl_projects SET bot_id=$3,bot_username=$4,encrypted_token=$5,
                    token_updated_at=$6,title=$7,version=version+1,updated_at=$6
                    WHERE id=$1 AND owner_tg_id=$2 RETURNING {_PROJECT_COLUMNS}""", pid, owner, bot_id, username, encrypted_token, now, title)
                if request["status"] == "pending" and self.payment_first_enabled:
                    # Project identity, immutable receipt's one-time bot bind,
                    # launch recovery and the managed request commit together.
                    # A crash cannot leave a paid bot without a recovery job.
                    await conn.execute("""UPDATE wl_entry_orders SET bot_id=$3
                        WHERE project_id=$1 AND id=$2 AND bot_id IS NULL
                          AND status='paid_pending_launch'""", pid, order["id"], bot_id)
                    await conn.execute("""INSERT INTO wl_launch_jobs
                        (project_id,order_id,owner_tg_id,bot_id,next_attempt_at)
                        VALUES($1,$2,$3,$4,$5)""", pid, order["id"], owner, bot_id, now)
                await conn.execute("""UPDATE wl_managed_bot_requests SET status='bound',last_update_id=$2,bound_at=$3
                    WHERE project_id=$1""", pid, update_id, now)
                await conn.execute("""INSERT INTO wl_project_audit(project_id,owner_tg_id,action,project_version,details)
                    VALUES($1,$2,'bot_connected',$3,$4::jsonb)""", pid, owner, row["version"], json.dumps({"fields": ["bot"]}))
                return _public(row)

    async def retry_job(self, project_id, owner_id, *, now):
        """Recover only a bot ID already reserved by a trusted Telegram event."""
        pid, owner = project_uuid(project_id), positive(owner_id)
        pool = await self.pool()
        async with pool.acquire() as conn:
            request = await conn.fetchrow("SELECT * FROM wl_managed_bot_requests WHERE project_id=$1 AND owner_tg_id=$2", pid, owner)
        if not request or request["status"] != "pending" or request["bot_id"] is None: return None
        # An uncommitted event has no durable sequence yet; reserve replay ID 0.
        return await self.claim_event(owner, request["bot_id"], 0, now=now)


class PartnerManagedBotService:
    def __init__(self, store, vault, manager_token, manager_username, telegram=None, enabled=False, allowed_user_ids=None, *, clock=None):
        if type(enabled) is not bool: raise ValueError("Managed bots require an explicit flag")
        self.store, self.vault, self._token, self.username = store, vault, manager_token, manager_username
        self.telegram = telegram if telegram is not None else TelegramManagedBotAdapter()
        self.enabled = enabled
        self.allowed_user_ids = normalize_manager_allowed_user_ids(allowed_user_ids)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self): return "<PartnerManagedBotService>"

    @property
    def available(self):
        try:
            validate_bot_token(self._token)
            return bool(self.enabled and re.fullmatch(r"[A-Za-z0-9_]{5,32}", self.username)
                        and callable(self.vault.encrypt)
                        and all(callable(getattr(self.telegram, k, None)) for k in ("get_me", "get_managed_bot_token"))
                        and all(callable(getattr(self.store, k, None)) for k in ("begin_request", "claim_event", "complete_binding")))
        except Exception: return False

    def allowed(self, owner):
        positive(owner)
        return self.allowed_user_ids is None or owner in self.allowed_user_ids

    async def request_creation(self, project_id, owner_id, expected_version):
        if not self.available or not self.allowed(owner_id): raise ManagedBotUnavailable("Создание бота пока недоступно.")
        manager = bot_identity(await self.telegram.get_me(self._token), int(self._token.split(":", 1)[0]))
        if manager.get("can_manage_bots") is not True or manager["username"].lower() != self.username.lower():
            raise ManagedBotUnavailable("Создание ботов нужно включить в Mini App BotFather.")
        now = self.clock()
        request = await self.store.begin_request(project_id, owner_id, expected_version, now=now, expires_at=now + timedelta(minutes=30))
        title = initial_name(request["title"]) or "Мой сервис"
        return {"status": "pending", "creation_url": "https://t.me/newbot/" + self.username + "?name=" + quote(title, safe=""),
                "expires_at": request["expires_at"].isoformat()}

    async def bind_job(self, job):
        token = await self.telegram.get_managed_bot_token(self._token, job["bot_id"])
        try:
            validate_bot_token(token)
            if int(token.split(":", 1)[0]) != job["bot_id"]: raise ValueError()
            bot = bot_identity(await self.telegram.get_me(token), job["bot_id"])
            encrypted = self.vault.encrypt(job["project_id"], job["bot_id"], token)
        except Exception:
            raise ManagedBotUnavailable("Telegram пока не подтвердил подключение. Обновите этот же проект.") from None
        await self.store.complete_binding(job, bot["username"], encrypted, first_name=bot.get("first_name"), now=self.clock())

    async def handle_update(self, update):
        identity = event_identity(update)
        if not self.available or identity is None: return False
        owner, bot_id, update_id = identity
        if not self.allowed(owner) or bot_id == int(self._token.split(":", 1)[0]): return False
        job = await self.store.claim_event(owner, bot_id, update_id, now=self.clock())
        if job is None: return False
        await self.bind_job(job)
        return True

    async def retry_pending(self, project_id, owner_id):
        if not self.available or not self.allowed(owner_id): raise ManagedBotUnavailable("Подключение недоступно.")
        job = await self.store.retry_job(project_id, owner_id, now=self.clock())
        if job is None: return False
        await self.bind_job(job)
        return True
