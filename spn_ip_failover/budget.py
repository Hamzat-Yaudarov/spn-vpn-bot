"""Shared, durable gross-spend cap for automated 1CENT orders.

Reservations are persisted before dispatch. Unresolved orders carry into later
days; deleting an IP or receiving a refund never replenishes this budget.
"""

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

from .provider import ProviderError
from .state import Journal


TIMEZONE = "Europe/Saratov"
PHASES = {"reserved", "submitting", "confirmed", "no_purchase"}
UNRESOLVED = {"reserved", "submitting"}


class BudgetError(ProviderError):
    pass


class BudgetDenied(BudgetError):
    """A known pre-dispatch denial: no paid UI action has been called."""


def money(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,7})(?:\.\d{1,2})?", value):
        raise ValueError("Сумма бюджета должна быть десятичной строкой в рублях")
    return Decimal(value)


def quoted_money(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or value.as_tuple().exponent < -2:
        raise BudgetError("Цена заказа не подтверждена в рублях с точностью до копейки")
    money(format(value, "f"))
    return value


@dataclass(frozen=True)
class BudgetSettings:
    journal: Path
    maximum_daily: Decimal
    timezone: str = TIMEZONE

    def __post_init__(self):
        if (not isinstance(self.journal, Path) or not self.journal.is_absolute()
                or self.timezone != TIMEZONE or not isinstance(self.maximum_daily, Decimal)
                or not self.maximum_daily.is_finite() or self.maximum_daily < 0
                or self.maximum_daily.as_tuple().exponent < -2):
            raise ValueError("Бюджет требует абсолютный журнал, Europe/Saratov и неотрицательный предел в рублях")
        money(format(self.maximum_daily, "f"))

    @classmethod
    def from_value(cls, value):
        if not isinstance(value, dict) or value.keys() - {"journal", "max_daily_rub", "timezone"}:
            raise ValueError("Неизвестные параметры общего бюджета 1Cent")
        return cls(Path(value["journal"]), money(value["max_daily_rub"]), value.get("timezone", TIMEZONE))


class AccountBudget:
    def __init__(self, settings, account, *, now=time.time):
        self.settings, self.now = settings, now
        self.account = hashlib.sha256(str(account).encode()).hexdigest()

    @staticmethod
    def key(server_id, request_id):
        return hashlib.sha256((server_id + "\x00" + request_id).encode()).hexdigest()

    def today(self):
        return datetime.fromtimestamp(self.now(), ZoneInfo(self.settings.timezone)).date().isoformat()

    @contextmanager
    def locked(self):
        # Different server adapters may have different order journals, but the
        # account budget is one short critical section. Never hold this lock
        # while calling the browser, network, or the order journal.
        for attempt in range(201):
            journal = Journal(self.settings.journal)
            try:
                journal.__enter__()
                break
            except BlockingIOError:
                if attempt == 200:
                    raise BudgetError("Общий журнал бюджета занят; платный запрос не отправлен") from None
                time.sleep(.01)
        try:
            yield journal
        finally:
            journal.__exit__(None, None, None)

    def _ledger(self, journal):
        ledger = journal.data.get("budget")
        if ledger is None:
            ledger = {"version": 1, "account": self.account, "timezone": self.settings.timezone, "orders": {}}
            journal.data["budget"] = ledger
        if (not isinstance(ledger, dict) or ledger.keys() != {"version", "account", "timezone", "orders"}
                or ledger["version"] != 1 or ledger["account"] != self.account
                or ledger["timezone"] != self.settings.timezone or not isinstance(ledger["orders"], dict)):
            raise BudgetError("Общий журнал бюджета относится к другому аккаунту или повреждён")
        for key, row in ledger["orders"].items():
            if (not isinstance(row, dict) or row.keys() != {"server_id", "request_id", "amount_rub", "days", "phase"}
                    or not isinstance(row["server_id"], str) or not isinstance(row["request_id"], str)
                    or self.key(row["server_id"], row["request_id"]) != key
                    or not isinstance(row["phase"], str) or row["phase"] not in PHASES
                    or not isinstance(row["days"], list) or not row["days"]
                    or not all(isinstance(day, str) for day in row["days"])
                    or len(set(row["days"])) != len(row["days"])):
                raise BudgetError("Повреждена запись общего бюджета 1Cent")
            try:
                money(row["amount_rub"])
                if any(not isinstance(day, str) or date.fromisoformat(day).isoformat() != day for day in row["days"]):
                    raise ValueError()
            except (ValueError, TypeError):
                raise BudgetError("Повреждена сумма или дата общего бюджета 1Cent") from None
        return ledger

    @staticmethod
    def _used(ledger, day):
        return sum((money(row["amount_rub"]) for row in ledger["orders"].values()
                    if day in row["days"] or row["phase"] in UNRESOLVED), Decimal("0.00"))

    def _enabled(self):
        if self.settings.maximum_daily <= 0:
            raise BudgetDenied("Автопокупки выключены: общий суточный бюджет 1Cent не задан")

    def record(self, server_id, request_id):
        with self.locked() as journal:
            return deepcopy(self._ledger(journal)["orders"].get(self.key(server_id, request_id)))

    def summary(self):
        with self.locked() as journal:
            ledger, day = self._ledger(journal), self.today()
            used = self._used(ledger, day)
            return {"day": day, "timezone": self.settings.timezone,
                    "maximum_rub": str(self.settings.maximum_daily), "used_rub": str(used),
                    "available_rub": str(max(Decimal("0.00"), self.settings.maximum_daily - used))}

    def check(self, amount):
        self._enabled()
        amount = quoted_money(amount)
        with self.locked() as journal:
            ledger = self._ledger(journal)
            if self._used(ledger, self.today()) + amount > self.settings.maximum_daily:
                raise BudgetDenied("Общий суточный бюджет 1Cent исчерпан; новых покупок не будет")

    def reserve(self, server_id, request_id, amount):
        self._enabled()
        amount, key = quoted_money(amount), self.key(server_id, request_id)
        with self.locked() as journal:
            ledger, day = self._ledger(journal), self.today()
            old = ledger["orders"].get(key)
            if old is not None:
                if (old["server_id"] != server_id or old["request_id"] != request_id
                        or money(old["amount_rub"]) != amount or old["phase"] != "reserved"):
                    raise BudgetError("Запрос уже учтён в бюджете; повторная покупка не разрешена")
                return key
            if self._used(ledger, day) + amount > self.settings.maximum_daily:
                raise BudgetDenied("Общий суточный бюджет 1Cent исчерпан; новых покупок не будет")
            ledger["orders"][key] = {"server_id": server_id, "request_id": request_id,
                                      "amount_rub": str(amount), "days": [day], "phase": "reserved"}
            journal.save()
            return key

    def transition(self, server_id, request_id, phase):
        if phase not in ("submitting", "confirmed", "no_purchase"):
            raise ValueError("Неизвестное состояние резерва бюджета")
        key = self.key(server_id, request_id)
        with self.locked() as journal:
            ledger, day = self._ledger(journal), self.today()
            row = ledger["orders"].get(key)
            if row is None:
                raise BudgetError("Резерв бюджета этого запроса не найден; покупку не повторяю")
            if row["phase"] == phase:
                return
            if row["phase"] in ("confirmed", "no_purchase"):
                raise BudgetError("Итог резерва бюджета уже зафиксирован; автоматической перезаписи нет")
            if phase == "submitting":
                self._enabled()
                if self._used(ledger, day) > self.settings.maximum_daily:
                    raise BudgetDenied("Общий бюджет изменён или исчерпан; платный запрос не отправлен")
            if day not in row["days"]:
                row["days"].append(day)
                row["days"].sort()
            row["phase"] = phase
            journal.save()

