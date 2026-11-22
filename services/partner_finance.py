"""Explicit, disabled-by-default partner accounting; no transfers or live settings.

Revenue is read exclusively from the verified-payment ledger. Withdrawals and
confirmed exceptional reversals share the wl_projects row lock, including the
last pre-transfer review. Financial amounts are integer kopecks throughout.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import json
import re
from uuid import UUID, uuid4

import asyncpg

MIN_WITHDRAWAL_KOPECKS = 150_000
MAX_MONEY_KOPECKS = 9_000_000_000_000_000  # Safe integer for the JSON client too.


class InvalidFinance(ValueError):
    pass


class FinanceConflict(RuntimeError):
    def __init__(self, message, *, code="finance_conflict"):
        super().__init__(message)
        self.code = code


class FinanceNotFound(LookupError):
    pass


class FinanceUnavailable(RuntimeError):
    pass


class FinanceForbidden(PermissionError):
    pass


def _uuid(value):
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError()
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise InvalidFinance("Некорректный идентификатор операции.") from None


def _integer(value, *, minimum=1, maximum=MAX_MONEY_KOPECKS):
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidFinance("Некорректная сумма или идентификатор.")
    return value


def _text(value, *, minimum=1, maximum=500):
    if not isinstance(value, str):
        raise InvalidFinance("Проверьте реквизиты и описание операции.")
    value = value.strip()
    if not minimum <= len(value) <= maximum or any(
        ord(c) < 32
        or ord(c) == 127
        or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        for c in value
    ):
        raise InvalidFinance("Проверьте реквизиты и описание операции.")
    return value


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InvalidFinance("Нужна дата с часовым поясом.")
    return value.astimezone(timezone.utc)


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def validate_requisites(method, value):
    """No claimed USDT network/rate: those remain explicit manual agreement."""
    if (
        not isinstance(method, str)
        or method not in {"sbp", "usdt"}
        or not isinstance(value, dict)
    ):
        raise InvalidFinance("Выберите СБП или USDT и укажите реквизиты.")
    if method == "sbp":
        if set(value) != {"bank", "phone"}:
            raise InvalidFinance("Для СБП нужны банк и телефон.")
        bank = _text(value["bank"], maximum=100)
        phone = _text(value["phone"], maximum=32)
        if not re.fullmatch(r"[+0-9() -]+", phone):
            raise InvalidFinance("Проверьте телефон для СБП.")
        digits = re.sub(r"\D", "", phone)
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if not re.fullmatch(r"7[0-9]{10}", digits):
            raise InvalidFinance("Укажите российский номер для СБП: +7 и 10 цифр.")
        return {"bank": bank, "phone": "+" + digits}
    if set(value) != {"address"}:
        raise InvalidFinance("Для USDT нужен адрес кошелька.")
    address = _text(value["address"], minimum=20, maximum=160)
    if not re.fullmatch(r"[A-Za-z0-9:_-]+", address):
        raise InvalidFinance("Проверьте адрес кошелька USDT.")
    return {"address": address}


def proportional_reversal(amount, partner, already_amount, already_partner, increment):
    """Cumulative half-up proportional rounding prevents partial-event drift."""
    for value in (amount, partner, already_amount, already_partner, increment):
        _integer(value, minimum=0)
    if (
        not amount
        or not increment
        or partner > amount
        or already_amount + increment > amount
    ):
        raise InvalidFinance("Сторно превышает подтверждённую оплату.")
    expected = (already_amount * partner + amount // 2) // amount
    if already_partner != expected:
        raise FinanceConflict("История сторно требует ручной сверки.")
    target = ((already_amount + increment) * partner + amount // 2) // amount
    return target - already_partner, increment - (target - already_partner)


class FinanceVault:
    """Separate encrypted-purpose envelopes; key rotation retains old keys.

    Not TokenVault: bank data is not a Telegram token. The same explicitly
    supplied Fernet keyring may be used, with authenticated purpose/tenant/id.
    """

    def __init__(self, keys):
        try:
            from cryptography.fernet import Fernet, MultiFernet

            if not keys:
                raise ValueError()
            raw = [key.encode("ascii") if isinstance(key, str) else key for key in keys]
            self._fernet = MultiFernet([Fernet(key) for key in raw])
            self._hash_keys = [
                hashlib.sha256(b"SPN-WL-finance-fingerprint-v1:" + key).digest()
                for key in raw
            ]
        except (ImportError, TypeError, ValueError, UnicodeError):
            raise FinanceUnavailable(
                "Защищённое хранение реквизитов не настроено."
            ) from None

    def __repr__(self):
        return "<FinanceVault>"

    def encrypt(self, project_id, identity, purpose, value):
        data = {
            "v": 1,
            "project": str(_uuid(project_id)),
            "id": str(_uuid(identity)),
            "purpose": purpose,
            "value": value,
        }
        return self._fernet.encrypt(_canonical(data)).decode("ascii")

    def decrypt(self, project_id, identity, purpose, encrypted):
        try:
            data = json.loads(self._fernet.decrypt(encrypted.encode("ascii")))
            if (
                data["v"] != 1
                or data["project"] != str(_uuid(project_id))
                or data["id"] != str(_uuid(identity))
                or data["purpose"] != purpose
            ):
                raise ValueError()
            return data["value"]
        except Exception:
            raise FinanceUnavailable(
                "Не удалось прочитать защищённые реквизиты."
            ) from None

    def fingerprints(self, payload):
        return [
            hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
            for key in self._hash_keys
        ]


def _withdrawal(row):
    return {
        key: row[key]
        for key in (
            "amount_kopecks",
            "method",
            "status",
            "destination_hint",
            "created_at",
            "updated_at",
            "paid_at",
            "rejection_reason",
        )
    } | {
        "id": str(row["id"]),
        "request_key": str(row["request_key"]),
        "number": f"P-{row['number']}",
    }


class PartnerFinanceStore:
    def __init__(
        self,
        pool_getter,
        *,
        vault=None,
        operator_ids=(),
        admin_recipient_id=None,
        enabled=False,
    ):
        if not callable(pool_getter):
            raise TypeError("pool_getter must be callable")
        self._pool_getter = pool_getter
        self.vault = vault
        self.operator_ids = frozenset(
            _integer(value, maximum=2**63 - 1) for value in operator_ids
        )
        self.admin_recipient_id = (
            None
            if admin_recipient_id is None
            else _integer(admin_recipient_id, maximum=2**63 - 1)
        )
        self.enabled = enabled is True

    def _enabled(self, *, writing=False):
        if not self.enabled:
            raise FinanceUnavailable("Финансы партнёров пока не включены.")
        if writing and (
            self.vault is None
            or not self.operator_ids
            or self.admin_recipient_id not in self.operator_ids
        ):
            raise FinanceUnavailable(
                "Получатель уведомлений и защищённое хранение не настроены."
            )

    def require_operator(self, operator_id):
        self._enabled()
        operator_id = _integer(operator_id, maximum=2**63 - 1)
        if operator_id not in self.operator_ids:
            raise FinanceForbidden("Недостаточно прав.")
        return operator_id

    async def _pool(self):
        result = self._pool_getter()
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    async def _project(conn, project_id, owner_id=None, *, lock=False):
        row = await conn.fetchrow(
            "SELECT id,owner_tg_id,status FROM wl_projects WHERE id=$1"
            + (" FOR UPDATE" if lock else ""),
            project_id,
        )
        if row is None or (owner_id is not None and row["owner_tg_id"] != owner_id):
            raise FinanceNotFound("Проект не найден.")
        return row

    @staticmethod
    async def _summary(conn, project_id):
        # One statement gives one coherent MVCC snapshot even at READ COMMITTED.
        row = await conn.fetchrow(
            """SELECT
          (SELECT COALESCE(sum(partner_kopecks),0)::bigint FROM wl_revenue_ledger WHERE project_id=$1) AS earned,
          (SELECT COALESCE(sum(partner_kopecks),0)::bigint FROM wl_revenue_reversals WHERE project_id=$1) AS reversed,
          (SELECT COALESCE(sum(amount_kopecks),0)::bigint FROM wl_withdrawals WHERE project_id=$1 AND status IN ('pending','processing')) AS reserved,
          (SELECT COALESCE(sum(amount_kopecks),0)::bigint FROM wl_withdrawals WHERE project_id=$1 AND status='paid') AS paid""",
            project_id,
        )
        earned, reversed_, reserved, paid = (
            int(row[key]) for key in ("earned", "reversed", "reserved", "paid")
        )
        balance = earned - reversed_ - reserved - paid
        return {
            "earned_kopecks": earned,
            "reversed_kopecks": reversed_,
            "net_earned_kopecks": earned - reversed_,
            "reserved_kopecks": reserved,
            "paid_kopecks": paid,
            "available_kopecks": max(0, balance),
            "debt_kopecks": max(0, -balance),
            "minimum_withdrawal_kopecks": MIN_WITHDRAWAL_KOPECKS,
            "methods": ["sbp", "usdt"],
        }

    async def summary(self, project_id, owner_id):
        self._enabled()
        project_id, owner_id = _uuid(project_id), _integer(owner_id, maximum=2**63 - 1)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, owner_id)
            return await self._summary(conn, project_id)

    async def list_withdrawals(self, project_id, owner_id, *, limit=100):
        self._enabled()
        project_id, owner_id = _uuid(project_id), _integer(owner_id, maximum=2**63 - 1)
        limit = _integer(limit, maximum=100)
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, owner_id)
            rows = await conn.fetch(
                "SELECT * FROM wl_withdrawals WHERE project_id=$1 ORDER BY created_at DESC,id DESC LIMIT $2",
                project_id,
                limit + 1,
            )
        return {
            "items": [_withdrawal(row) for row in rows[:limit]],
            "truncated": len(rows) > limit,
        }

    async def get_withdrawal(self, project_id, owner_id, request_key):
        self._enabled()
        project_id, owner_id, request_key = (
            _uuid(project_id),
            _integer(owner_id, maximum=2**63 - 1),
            _uuid(request_key),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, owner_id)
            row = await conn.fetchrow(
                "SELECT * FROM wl_withdrawals WHERE project_id=$1 AND request_key=$2",
                project_id,
                request_key,
            )
        if row is None:
            raise FinanceNotFound("Заявка не найдена.")
        return _withdrawal(row)

    @staticmethod
    async def _audit(
        conn,
        project_id,
        actor,
        action,
        amount,
        now,
        *,
        withdrawal_id=None,
        reversal_id=None,
    ):
        await conn.execute(
            """INSERT INTO wl_finance_audit(project_id,withdrawal_id,reversal_id,actor_tg_id,action,amount_kopecks,created_at)
            VALUES($1,$2,$3,$4,$5,$6,$7)""",
            project_id,
            withdrawal_id,
            reversal_id,
            actor,
            action,
            amount,
            now,
        )

    @staticmethod
    async def _notify(conn, project_id, event_id, kind, recipient, now):
        await conn.execute(
            """INSERT INTO wl_finance_outbox(project_id,event_id,kind,recipient_tg_id,created_at,next_attempt_at)
            VALUES($1,$2,$3,$4,$5,$5)""",
            project_id,
            event_id,
            kind,
            recipient,
            now,
        )

    async def request_withdrawal(
        self,
        project_id,
        owner_id,
        request_key,
        amount_kopecks,
        method,
        requisites,
        *,
        now=None,
    ):
        self._enabled(writing=True)
        project_id, owner_id, request_key = (
            _uuid(project_id),
            _integer(owner_id, maximum=2**63 - 1),
            _uuid(request_key),
        )
        amount_kopecks = _integer(amount_kopecks, minimum=MIN_WITHDRAWAL_KOPECKS)
        requisites, now = validate_requisites(method, requisites), _now(now)
        payload = {
            "project": str(project_id),
            "owner": owner_id,
            "amount": amount_kopecks,
            "method": method,
            "requisites": requisites,
        }
        fingerprints = self.vault.fingerprints(payload)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                project = await self._project(conn, project_id, owner_id, lock=True)
                old = await conn.fetchrow(
                    "SELECT * FROM wl_withdrawals WHERE project_id=$1 AND request_key=$2",
                    project_id,
                    request_key,
                )
                if old:
                    if old["request_fingerprint"] not in fingerprints:
                        raise FinanceConflict(
                            "Ключ запроса уже использован с другими данными.",
                            code="request_conflict",
                        )
                    return _withdrawal(old)
                if project["status"] != "active":
                    raise FinanceUnavailable("Для новой заявки нужен активный проект.")
                pending = await conn.fetchval(
                    "SELECT id FROM wl_withdrawals WHERE project_id=$1 AND status IN ('pending','processing') LIMIT 1",
                    project_id,
                )
                if pending:
                    raise FinanceConflict(
                        "Уже есть незавершённая заявка. Проверьте её состояние в истории.",
                        code="withdrawal_pending",
                    )
                summary = await self._summary(conn, project_id)
                if (
                    summary["debt_kopecks"]
                    or summary["available_kopecks"] < amount_kopecks
                ):
                    raise FinanceConflict(
                        "Недостаточно доступных средств для этой заявки.",
                        code="insufficient_balance",
                    )
                identity = uuid4()
                encrypted = self.vault.encrypt(
                    project_id, identity, "requisites", requisites
                )
                hint = (
                    (
                        "СБП · "
                        + requisites["bank"][:85]
                        + " · ••••"
                        + requisites["phone"][-4:]
                    )
                    if method == "sbp"
                    else "USDT · ••••" + requisites["address"][-6:]
                )
                row = await conn.fetchrow(
                    """INSERT INTO wl_withdrawals(id,project_id,owner_tg_id,request_key,request_fingerprint,
                    amount_kopecks,method,encrypted_requisites,destination_hint,created_at,updated_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10) RETURNING *""",
                    identity,
                    project_id,
                    owner_id,
                    request_key,
                    fingerprints[0],
                    amount_kopecks,
                    method,
                    encrypted,
                    hint,
                    now,
                )
                await self._audit(
                    conn,
                    project_id,
                    owner_id,
                    "withdrawal_requested",
                    amount_kopecks,
                    now,
                    withdrawal_id=identity,
                )
                await self._notify(
                    conn,
                    project_id,
                    identity,
                    "withdrawal_requested",
                    self.admin_recipient_id,
                    now,
                )
                return _withdrawal(row)

    async def list_operator_withdrawals(self, operator_id, *, limit=100):
        self.require_operator(operator_id)
        limit = _integer(limit, maximum=100)
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT w.*,p.title AS project_title FROM wl_withdrawals w JOIN wl_projects p ON p.id=w.project_id
                ORDER BY CASE w.status WHEN 'processing' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,w.created_at,w.id LIMIT $1""",
                limit + 1,
            )
        return {
            "items": [
                _withdrawal(row)
                | {
                    "project_id": str(row["project_id"]),
                    "project_title": row["project_title"],
                }
                for row in rows[:limit]
            ],
            "truncated": len(rows) > limit,
        }

    async def _operator_row(self, conn, project_id, withdrawal_id):
        await self._project(conn, project_id, lock=True)
        row = await conn.fetchrow(
            "SELECT * FROM wl_withdrawals WHERE project_id=$1 AND id=$2 FOR UPDATE",
            project_id,
            withdrawal_id,
        )
        if row is None:
            raise FinanceNotFound("Заявка не найдена.")
        return row

    async def operator_details(
        self, project_id, withdrawal_id, operator_id, *, now=None
    ):
        operator_id = self.require_operator(operator_id)
        self._enabled(writing=True)
        project_id, identity, now = _uuid(project_id), _uuid(withdrawal_id), _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await self._operator_row(conn, project_id, identity)
                requisites = self.vault.decrypt(
                    project_id, identity, "requisites", row["encrypted_requisites"]
                )
                reference = None
                if row["encrypted_transfer_reference"]:
                    reference = self.vault.decrypt(
                        project_id,
                        identity,
                        "transfer",
                        row["encrypted_transfer_reference"],
                    )
                await self._audit(
                    conn,
                    project_id,
                    operator_id,
                    "withdrawal_details_viewed",
                    0,
                    now,
                    withdrawal_id=identity,
                )
                return _withdrawal(row) | {
                    "project_id": str(project_id),
                    "requisites": requisites,
                    "transfer_reference": reference,
                    "summary": await self._summary(conn, project_id),
                }

    async def claim_withdrawal(
        self, project_id, withdrawal_id, operator_id, *, now=None
    ):
        operator_id = self.require_operator(operator_id)
        self._enabled(writing=True)
        project_id, identity, now = _uuid(project_id), _uuid(withdrawal_id), _now(now)
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await self._operator_row(conn, project_id, identity)
                if (
                    row["status"] == "processing"
                    and row["processing_operator_id"] == operator_id
                ):
                    return _withdrawal(row)
                if row["status"] != "pending":
                    raise FinanceConflict("Заявка уже обрабатывается или завершена.")
                if (await self._summary(conn, project_id))["debt_kopecks"]:
                    raise FinanceConflict(
                        "После сторно нужна сверка баланса; выплата запрещена."
                    )
                row = await conn.fetchrow(
                    """UPDATE wl_withdrawals SET status='processing',processing_operator_id=$3,updated_at=$4
                    WHERE project_id=$1 AND id=$2 RETURNING *""",
                    project_id,
                    identity,
                    operator_id,
                    now,
                )
                await self._audit(
                    conn,
                    project_id,
                    operator_id,
                    "withdrawal_claimed",
                    row["amount_kopecks"],
                    now,
                    withdrawal_id=identity,
                )
                return _withdrawal(row)

    async def confirm_withdrawal_paid(
        self,
        project_id,
        withdrawal_id,
        operator_id,
        actual_amount_kopecks,
        transfer_reference,
        *,
        now=None,
    ):
        """Records an ALREADY executed manual transfer, never initiates one.

        A reversal after claim can create debt. Do not hide a transfer that really
        happened: record the historical fact, retaining the resulting debt.
        The operator must recheck details immediately before transferring.
        """
        operator_id = self.require_operator(operator_id)
        self._enabled(writing=True)
        project_id, identity, now = _uuid(project_id), _uuid(withdrawal_id), _now(now)
        actual_amount_kopecks = _integer(
            actual_amount_kopecks, minimum=MIN_WITHDRAWAL_KOPECKS
        )
        reference = _text(transfer_reference, minimum=6, maximum=200)
        reference_hash = hashlib.sha256(reference.encode()).hexdigest()
        pool = await self._pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await self._operator_row(conn, project_id, identity)
                    if actual_amount_kopecks != row["amount_kopecks"]:
                        raise FinanceConflict(
                            "Нужно подтвердить точную сумму заявки; частичные выплаты не поддерживаются."
                        )
                    if row["status"] == "paid":
                        if row["transfer_reference_hash"] != reference_hash:
                            raise FinanceConflict(
                                "Эта выплата уже подтверждена с другим основанием."
                            )
                        return _withdrawal(row)
                    if (
                        row["status"] != "processing"
                        or row["processing_operator_id"] != operator_id
                    ):
                        raise FinanceConflict(
                            "Сначала возьмите заявку в обработку этим оператором."
                        )
                    row = await conn.fetchrow(
                        """UPDATE wl_withdrawals SET status='paid',paid_kopecks=$3,
                        transfer_reference_hash=$4,encrypted_transfer_reference=$5,paid_at=$6,updated_at=$6
                        WHERE project_id=$1 AND id=$2 RETURNING *""",
                        project_id,
                        identity,
                        actual_amount_kopecks,
                        reference_hash,
                        self.vault.encrypt(project_id, identity, "transfer", reference),
                        now,
                    )
                    await self._audit(
                        conn,
                        project_id,
                        operator_id,
                        "withdrawal_paid",
                        actual_amount_kopecks,
                        now,
                        withdrawal_id=identity,
                    )
                    await self._notify(
                        conn,
                        project_id,
                        identity,
                        "withdrawal_paid",
                        row["owner_tg_id"],
                        now,
                    )
                    return _withdrawal(row)
        except asyncpg.UniqueViolationError:
            raise FinanceConflict(
                "Это подтверждение перевода уже использовано."
            ) from None

    async def reject_withdrawal(
        self,
        project_id,
        withdrawal_id,
        operator_id,
        reason,
        *,
        confirmed_no_transfer=False,
        now=None,
    ):
        operator_id = self.require_operator(operator_id)
        self._enabled(writing=True)
        project_id, identity, now = _uuid(project_id), _uuid(withdrawal_id), _now(now)
        reason = _text(reason, maximum=500)
        if type(confirmed_no_transfer) is not bool:
            raise InvalidFinance("Подтверждение отсутствия перевода должно быть явным.")
        pool = await self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await self._operator_row(conn, project_id, identity)
                if row["status"] == "rejected":
                    if row["rejection_reason"] != reason:
                        raise FinanceConflict("Заявка уже отклонена с другой причиной.")
                    return _withdrawal(row)
                processing_release = (
                    row["status"] == "processing"
                    and row["processing_operator_id"] == operator_id
                    and confirmed_no_transfer
                )
                if row["status"] != "pending" and not processing_release:
                    # A bank transfer may already have happened. Only the same
                    # claiming operator can attest a verified no-transfer fact.
                    raise FinanceConflict(
                        "Нельзя освободить резерв обрабатываемой или оплаченной заявки."
                    )
                row = await conn.fetchrow(
                    """UPDATE wl_withdrawals SET status='rejected',rejection_reason=$3,updated_at=$4,no_transfer_confirmed=$5
                    WHERE project_id=$1 AND id=$2 RETURNING *""",
                    project_id,
                    identity,
                    reason,
                    now,
                    processing_release,
                )
                await self._audit(
                    conn,
                    project_id,
                    operator_id,
                    "withdrawal_rejected",
                    row["amount_kopecks"],
                    now,
                    withdrawal_id=identity,
                )
                await self._notify(
                    conn,
                    project_id,
                    identity,
                    "withdrawal_rejected",
                    row["owner_tg_id"],
                    now,
                )
                return _withdrawal(row)

    async def confirm_reversal(
        self,
        project_id,
        order_id,
        operator_id,
        request_key,
        amount_kopecks,
        *,
        kind,
        event_reference,
        reason,
        now=None,
    ):
        """Append only a confirmed provider/bank fact; no refund request/action."""
        operator_id = self.require_operator(operator_id)
        self._enabled(writing=True)
        project_id, order_id, request_key = (
            _uuid(project_id),
            _uuid(order_id),
            _uuid(request_key),
        )
        amount_kopecks, now = _integer(amount_kopecks), _now(now)
        if kind not in {"chargeback", "confirmed_refund"}:
            raise InvalidFinance("Нужен подтверждённый возврат или чарджбэк.")
        reference = _text(event_reference, minimum=6, maximum=200)
        reason = _text(reason, maximum=1000)
        payload = {
            "project": str(project_id),
            "order": str(order_id),
            "amount": amount_kopecks,
            "kind": kind,
            "reference": reference,
            "reason": reason,
        }
        fingerprints = self.vault.fingerprints(payload)
        event_hash = hashlib.sha256(reference.encode()).hexdigest()
        pool = await self._pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self._project(conn, project_id, lock=True)
                    old = await conn.fetchrow(
                        "SELECT * FROM wl_revenue_reversals WHERE project_id=$1 AND request_key=$2",
                        project_id,
                        request_key,
                    )
                    if old:
                        if old["request_fingerprint"] not in fingerprints:
                            raise FinanceConflict(
                                "Ключ сторно уже использован с другими данными."
                            )
                        return self._reversal(old)
                    old_event = await conn.fetchrow(
                        "SELECT * FROM wl_revenue_reversals WHERE event_reference_hash=$1",
                        event_hash,
                    )
                    if old_event:
                        # A new UI key cannot duplicate the same external fact.
                        if (
                            str(old_event["project_id"]) == str(project_id)
                            and old_event["request_fingerprint"] in fingerprints
                        ):
                            return self._reversal(old_event)
                        raise FinanceConflict("Это основание сторно уже использовано.")
                    ledger = await conn.fetchrow(
                        "SELECT * FROM wl_revenue_ledger WHERE project_id=$1 AND order_id=$2",
                        project_id,
                        order_id,
                    )
                    if ledger is None:
                        raise FinanceNotFound("Подтверждённая оплата не найдена.")
                    prior = await conn.fetchrow(
                        """SELECT COALESCE(sum(amount_kopecks),0)::bigint AS amount,
                        COALESCE(sum(partner_kopecks),0)::bigint AS partner FROM wl_revenue_reversals WHERE project_id=$1 AND order_id=$2""",
                        project_id,
                        order_id,
                    )
                    partner, platform = proportional_reversal(
                        ledger["amount_kopecks"],
                        ledger["partner_kopecks"],
                        prior["amount"],
                        prior["partner"],
                        amount_kopecks,
                    )
                    identity = uuid4()
                    row = await conn.fetchrow(
                        """INSERT INTO wl_revenue_reversals(id,project_id,order_id,request_key,request_fingerprint,
                        event_reference_hash,encrypted_evidence,kind,amount_kopecks,partner_kopecks,platform_kopecks,operator_tg_id,created_at)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *""",
                        identity,
                        project_id,
                        order_id,
                        request_key,
                        fingerprints[0],
                        event_hash,
                        self.vault.encrypt(
                            project_id,
                            identity,
                            "reversal",
                            {"reference": reference, "reason": reason},
                        ),
                        kind,
                        amount_kopecks,
                        partner,
                        platform,
                        operator_id,
                        now,
                    )
                    await self._audit(
                        conn,
                        project_id,
                        operator_id,
                        "reversal_confirmed",
                        partner,
                        now,
                        reversal_id=identity,
                    )
                    await self._notify(
                        conn,
                        project_id,
                        identity,
                        "reversal_confirmed",
                        self.admin_recipient_id,
                        now,
                    )
                    return self._reversal(row)
        except asyncpg.UniqueViolationError:
            raise FinanceConflict(
                "Подтверждённое сторно уже учтено; обновите данные."
            ) from None

    @staticmethod
    def _reversal(row):
        return {
            key: row[key]
            for key in (
                "kind",
                "amount_kopecks",
                "partner_kopecks",
                "platform_kopecks",
                "created_at",
            )
        } | {
            "id": str(row["id"]),
            "order_id": str(row["order_id"]),
            "request_key": str(row["request_key"]),
        }

    async def list_reversals(self, project_id, owner_id, *, limit=100):
        self._enabled()
        project_id, owner_id, limit = (
            _uuid(project_id),
            _integer(owner_id, maximum=2**63 - 1),
            _integer(limit, maximum=100),
        )
        pool = await self._pool()
        async with pool.acquire() as conn:
            await self._project(conn, project_id, owner_id)
            rows = await conn.fetch(
                "SELECT * FROM wl_revenue_reversals WHERE project_id=$1 ORDER BY created_at DESC,id DESC LIMIT $2",
                project_id,
                limit + 1,
            )
        return {
            "items": [self._reversal(row) for row in rows[:limit]],
            "truncated": len(rows) > limit,
        }
