"""Use the 1Cent billing interface for one-address orders and scoped cleanup."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from .deploy.provider_ui import private_profile
from .budget import AccountBudget, BudgetDenied, BudgetError, BudgetSettings, quoted_money
from .onecent_auth import OneCentLogin, OneCentLoginError
from .onecent_fields import normalize_service_id
from .provider import Allocation, ProviderError, _unique_object
from .state import Journal


ORIGIN = "https://bill.1cent.host"
# The normalized-receipt framework is not a claim that the live provider's
# response schema has been reviewed and wired into the browser adapter.
RECEIPT_CORRELATED_OWNERSHIP = False
PAID_FLOW_CORRELATED_OWNERSHIP = True
IDEMPOTENT_RELEASE = True
OWNERSHIP_POLICY = "exclusive_paid_flow_inventory_v1"


def rubles(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,7})(?:\.\d{1,2})?", value):
        raise ValueError("Лимит в рублях должен быть десятичной строкой")
    return Decimal(value)


@dataclass(frozen=True)
class Service:
    name: str
    maximum_upfront: Decimal
    maximum_monthly: Decimal
    existing_removable: frozenset
    ownership_policy: str | None = None

    def __post_init__(self):
        if self.ownership_policy not in (None, OWNERSHIP_POLICY):
            raise ValueError("Неизвестная политика доказательства владения IPv4 1Cent")


@dataclass(frozen=True)
class Settings:
    profile: Path
    journal: Path
    servers: dict
    browser_channel: str | None = None
    budget: BudgetSettings | None = None

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Конфигурация 1Cent слишком велика")
        config = json.loads(raw, object_pairs_hook=_unique_object)
        profile, journal = Path(config["profile"]), Path(config["journal"])
        if not profile.is_absolute() or not journal.is_absolute() or profile == journal:
            raise ValueError("1Cent требует раздельных абсолютных путей профиля и журнала")
        browser_channel = config.get("browser_channel")
        if browser_channel not in (None, "chrome"):
            raise ValueError("1Cent поддерживает Chromium или установленный официальный Chrome")
        servers = {}
        for key, value in config["servers"].items():
            name = value["name"]
            if not isinstance(name, str) or name != name.strip() or not 1 <= len(name) <= 150:
                raise ValueError("Нужно точное имя услуги 1Cent из кабинета")
            removable = value.get("removable_existing_additional_ipv4", [])
            if not isinstance(removable, list):
                raise ValueError("Список разрешённых к освобождению IPv4 должен быть массивом")
            servers[normalize_service_id(key)] = Service(
                name, rubles(value["max_upfront_rub"]), rubles(value["max_monthly_rub"]),
                frozenset(Allocation(ip, ip).ip for ip in removable),
                value.get("ownership_policy"),
            )
        if not servers:
            raise ValueError("Нужен хотя бы один VPS 1Cent")
        budget = BudgetSettings.from_value(config["budget"]) if config.get("budget") is not None else None
        if budget and budget.journal.resolve() in (profile.resolve(), journal.resolve()):
            raise ValueError("Общий бюджет 1Cent требует отдельного журнала, не профиля браузера или журнала заказов")
        return cls(profile, journal, servers, browser_channel, budget)


class OneCentBilling:
    def __init__(self, settings, server_id, ui, *, sleep=time.sleep, now=time.time):
        self.settings = settings
        self.server_id = normalize_service_id(server_id)
        self.service = settings.servers[self.server_id]
        self.ui = ui
        self.sleep = sleep
        self.now = now

    def account_budget(self, *, for_purchase=False):
        if self.settings.budget is None:
            error = BudgetDenied if for_purchase else BudgetError
            raise error("Автопокупки выключены: настрой общий суточный бюджет 1Cent")
        return AccountBudget(self.settings.budget, self.settings.profile.resolve(), now=self.now)

    def entry_budget(self, entry):
        marker = entry.get("budget")
        if marker is None:
            return None  # Historical orders are not silently recalculated.
        budget = self.account_budget()
        if marker != {"journal": str(self.settings.budget.journal.resolve()),
                      "key": budget.key(self.server_id, entry["request_id"])}:
            raise BudgetError("Для прежнего заказа изменён общий журнал бюджета; восстанови его конфигурацию")
        return budget

    def inventory(self):
        return {row.ip: row.primary for row in self.ui.inventory()}

    def quote(self):
        quote = self.ui.open_quote()
        quoted_money(quote.upfront_rub)
        quoted_money(quote.monthly_rub)
        if (quote.upfront_rub > self.service.maximum_upfront
                or quote.monthly_rub > self.service.maximum_monthly):
            raise BudgetDenied("Цена 1Cent выше настроенного предела; платный запрос не отправлен")
        return quote

    def orders(self, journal):
        return journal.data["servers"].setdefault(self.server_id, {"orders": {}, "releases": {}})

    @staticmethod
    def idempotency_key(server_id, request_id):
        return str(uuid5(NAMESPACE_URL, ORIGIN + "/api/services/" + server_id + "/ipv4/add#" + request_id))

    def require_ownership_policy(self):
        # A response with address:null cannot distinguish a concurrent manual
        # addition. This explicit per-service opt-in promises exclusive address
        # mutation during the guarded paid flow; the browser guard alone cannot.
        if self.service.ownership_policy != OWNERSHIP_POLICY:
            raise BudgetDenied("Автопокупки 1Cent требуют явной политики исключительных изменений IPv4")

    def paid_response(self, value, entry):
        """Verify the exact paid POST evidence without inventing a receipt IP."""
        fields = {"format", "basis", "request_id", "service_id", "variant_id", "idempotency_key",
                  "method", "path", "http_status", "pending", "address", "amount_rub", "response_sha256"}
        if (not isinstance(value, dict) or value.keys() != fields
                or type(value["format"]) is not int or value["format"] != 1
                or value["basis"] != "guarded_paid_post_v1"
                or value["request_id"] != entry["request_id"] or value["service_id"] != self.server_id
                or value["variant_id"] != entry.get("variant_id")
                or not isinstance(value["variant_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value["variant_id"])
                or value["idempotency_key"] != entry.get("idempotency_key")
                or value["idempotency_key"] != self.idempotency_key(self.server_id, entry["request_id"])
                or value["method"] != "POST" or value["path"] != f"/api/services/{self.server_id}/ipv4/add"
                or type(value["http_status"]) is not int or value["http_status"] != 200
                or value["pending"] is not False or value["address"] is not None
                or not isinstance(value["amount_rub"], str)
                or not re.fullmatch(r"(?:0|[1-9]\d{0,7})\.\d{2}", value["amount_rub"])
                or Decimal(value["amount_rub"]) != Decimal(entry["upfront_rub"])
                or not isinstance(value["response_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["response_sha256"])):
            raise ProviderError("Нет точного подтверждения платного ответа IPv4 1Cent")
        return dict(value)

    def entry_ownership(self, entry):
        self.paid_response(entry.get("paid_response"), entry)
        proof = entry.get("ownership")
        fields = {"basis", "ip", "before", "after", "stable_reads", "provider_idle",
                  "provider_response_address", "paid_response_sha256"}
        if (entry.get("ownership_policy") != OWNERSHIP_POLICY or not isinstance(proof, dict)
                or proof.keys() != fields or proof["basis"] != OWNERSHIP_POLICY
                or type(proof["stable_reads"]) is not int or proof["stable_reads"] != 2
                or proof["provider_idle"] is not True or proof["provider_response_address"] is not None
                or proof["before"] != entry["before"]
                or proof["paid_response_sha256"] != entry["paid_response"]["response_sha256"]):
            raise ProviderError("Нет сохранённого доказательства согласованного платного потока IPv4 1Cent")
        try:
            Allocation(proof["ip"], proof["ip"])
        except (TypeError, ValueError):
            raise ProviderError("В доказательстве 1Cent нет публичного IPv4") from None
        if (proof["ip"] in entry["before"] or proof["after"] != {**entry["before"], proof["ip"]: False}
                or entry.get("ip", proof["ip"]) != proof["ip"]):
            raise ProviderError("Доказательство IPv4 1Cent не соответствует сохранённому заказу")
        return proof

    def owns(self, entry, ip):
        if entry.get("phase") != "confirmed" or entry.get("ip") != ip:
            return False
        try:
            return self.entry_ownership(entry)["ip"] == ip
        except (KeyError, TypeError, ValueError, ProviderError):
            # Historical delta-only confirmations never authorize deletion.
            return False

    def record_paid_response(self, request_id, response):
        """Persist reviewed financial evidence only; it never grants an IP."""
        request_id = self.request_id(request_id)
        with Journal(self.settings.journal) as journal:
            entry = self.orders(journal)["orders"].get(request_id)
            if not entry or entry.get("request_id") != request_id or entry["phase"] != "submitting":
                raise ProviderError("Нет неподтверждённого заказа 1Cent с таким ID")
            proof = self.paid_response(response, entry)
            if "paid_response" in entry and entry["paid_response"] != proof:
                raise ProviderError("Для заказа 1Cent уже сохранён другой платный ответ")
            entry["paid_response"] = proof
            journal.save()

    def allocate(self, request_id):
        request_id = self.request_id(request_id)
        with Journal(self.settings.journal) as journal:
            entries = self.orders(journal)["orders"]
            entry = entries.get(request_id)
            if entry:
                if entry.get("request_id") != request_id:
                    raise ProviderError("Сохранённый запрос 1Cent относится к другому ID")
                if entry["phase"] == "confirmed":
                    proof = self.entry_ownership(entry)
                    if proof["ip"] not in self.inventory():
                        raise ProviderError("Подтверждённый IPv4 1Cent отсутствует в кабинете")
                    budget = self.entry_budget(entry)
                    if budget:
                        budget.transition(self.server_id, request_id, "confirmed")
                    return Allocation(entry["ip"], entry["ip"])
                if entry["phase"] == "not_submitted":
                    raise BudgetDenied("Платный запрос не отправлялся; используй новый запрос после восстановления бюджета")
                raise ProviderError("Запрос 1Cent уже записан. Повторного заказа не будет")
            if any(item["phase"] == "submitting" for item in entries.values()):
                raise ProviderError("В 1Cent есть неподтверждённый заказ этого VPS")
            self.require_ownership_policy()
            budget = self.account_budget(for_purchase=True)
            budget._enabled()
            before = self.inventory()
            if len(before) >= 10:
                raise ProviderError("На VPS 1Cent уже назначено десять IPv4")
            quote = self.quote()
            if self.inventory() != before:
                raise ProviderError("Список IPv4 изменился во время подготовки заказа 1Cent")
            variant_id = self.ui.order_variant_id()
            if (not isinstance(variant_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", variant_id)):
                raise ProviderError("Вариант платного заказа IPv4 1Cent не подтверждён")
            key = budget.reserve(self.server_id, request_id, quote.upfront_rub)
            entries[request_id] = {"phase": "submitting", "before": before, "request_id": request_id,
                                   "variant_id": variant_id,
                                   "ownership_policy": OWNERSHIP_POLICY,
                                   "idempotency_key": self.idempotency_key(self.server_id, request_id),
                                   "budget": {"journal": str(self.settings.budget.journal.resolve()), "key": key},
                                   "upfront_rub": str(quote.upfront_rub), "monthly_rub": str(quote.monthly_rub)}
            journal.save()
            try:
                # The second durable marker is the boundary before a click can
                # have happened. An unknown result never releases this amount.
                budget.transition(self.server_id, request_id, "submitting")
            except BudgetDenied:
                budget.transition(self.server_id, request_id, "no_purchase")
                entries[request_id]["phase"] = "not_submitted"
                journal.save()
                raise
            entry = entries[request_id]
            response = self.ui.submit_order(quote, request_id=request_id, idempotency_key=entry["idempotency_key"])
            proof = self.paid_response(response, entry)
            entry["paid_response"] = proof
            journal.save()
            ip, after = self.wait_for_purchase(before)
            entry["ownership"] = {"basis": OWNERSHIP_POLICY, "ip": ip, "before": before, "after": after,
                                  "stable_reads": 2, "provider_idle": True, "provider_response_address": None,
                                  "paid_response_sha256": proof["response_sha256"]}
            journal.save()
            budget.transition(self.server_id, request_id, "confirmed")
            entries[request_id].update(phase="confirmed", ip=ip)
            journal.save()
            return Allocation(ip, ip)

    def wait_for_purchase(self, before, expected_ip=None):
        # Called only after a guarded paid response was durably recorded under
        # the explicit exclusivity policy, never as inventory-only ownership.
        observed = None
        for _ in range(6):
            self.sleep(2)
            self.ui.open()
            current = self.inventory()
            added = current.keys() - before.keys()
            if (any(current.get(ip) is not primary for ip, primary in before.items()) or len(added) > 1):
                raise ProviderError("Список IPv4 1Cent изменился во время исключительного платного потока")
            ip = next(iter(added), None)
            if ip is not None and (current[ip] is not False or expected_ip not in (None, ip)):
                raise ProviderError("1Cent не подтвердил единственный новый дополнительный IPv4")
            if observed is not None:
                if ip != observed:
                    raise ProviderError("Новый IPv4 1Cent изменился при повторной проверке")
                return ip, current
            observed = ip
        raise ProviderError("1Cent не подтвердил выдачу IPv4. Проверь заказ в кабинете")

    def release_response(self, value, ip, idempotency_key):
        fields = {"format", "basis", "service_id", "idempotency_key", "method", "path", "http_status",
                  "released", "refunded_rub", "response_sha256"}
        if (not isinstance(value, dict) or value.keys() != fields or type(value["format"]) is not int
                or value["format"] != 1 or value["basis"] != "guarded_release_v1"
                or value["service_id"] != self.server_id or value["idempotency_key"] != idempotency_key
                or value["method"] != "DELETE" or type(value["http_status"]) is not int
                or value["http_status"] != 200 or value["released"] != ip
                or not isinstance(value["path"], str)
                or re.fullmatch(r"/api/services/" + re.escape(self.server_id) + r"/ipv4/(?:[0-9]|1[0-5])", value["path"]) is None
                or not isinstance(value["refunded_rub"], str)
                or not re.fullmatch(r"(?:0|[1-9]\d{0,7})\.\d{2}", value["refunded_rub"])
                or not isinstance(value["response_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["response_sha256"])):
            raise ProviderError("Удаление IPv4 1Cent не подтверждено точным платёжным ответом")
        return dict(value)

    def release(self, target, *, replacement=None):
        if target.ref != target.ip or (replacement and replacement.ref != replacement.ip):
            raise ProviderError("Идентификатор IPv4 в 1Cent должен совпадать с адресом")
        with Journal(self.settings.journal) as journal:
            state = self.orders(journal)
            owned = [entry for entry in state["orders"].values()
                     if self.owns(entry, target.ip)]
            current = self.inventory()
            pending = state["releases"].get(target.ip)
            if replacement is not None and (replacement.ip == target.ip or replacement.ip not in current):
                raise ProviderError("В кабинете 1Cent не подтверждён действующий IPv4")
            if target.ip not in current:
                if isinstance(pending, dict) and current != pending["expected"]:
                    raise ProviderError("Итоговые адреса 1Cent отличаются от ожидаемых после удаления")
                if owned or pending:
                    self.record_release(state, owned, target.ip, journal)
                return
            if isinstance(pending, dict) or pending == "submitting":
                raise ProviderError("Удаление IPv4 1Cent не подтверждено. Проверь его в кабинете")
            primary = current[target.ip]
            if primary:
                if (replacement is None or current != {target.ip: True, replacement.ip: False}
                        or not any(self.owns(entry, replacement.ip) for entry in state["orders"].values())):
                    raise ProviderError("Смена главного IPv4 1Cent требует ровно двух адресов и проверенной замены")
                expected = {replacement.ip: True}
            elif len(owned) > 1 or (not owned and target.ip not in self.service.existing_removable):
                raise ProviderError("Можно освободить только подтверждённый дополнительный IPv4 1Cent")
            else:
                expected = {ip: flag for ip, flag in current.items() if ip != target.ip}
            release_key = str(uuid4())
            def before_submit():
                state["releases"][target.ip] = {"before": current, "expected": expected,
                                                "target_ip": target.ip, "idempotency_key": release_key}
                journal.save()

            if primary:
                response = self.ui.remove_primary(target.ip, replacement.ip, before_submit=before_submit,
                                                  idempotency_key=release_key, expected_inventory=current)
            else:
                response = self.ui.remove_additional(target.ip, before_submit=before_submit,
                                                     idempotency_key=release_key, expected_inventory=current)
            proof = self.release_response(response, target.ip, release_key)
            pending = state["releases"].get(target.ip)
            if not isinstance(pending, dict) or pending.get("idempotency_key") != release_key:
                raise ProviderError("Удаление 1Cent не имеет предварительно сохранённого ключа")
            pending["response"] = proof
            journal.save()
            for _ in range(6):
                self.sleep(2)
                self.ui.open()
                after = self.inventory()
                if after == expected:
                    self.record_release(state, owned, target.ip, journal)
                    return
                if after != current:
                    raise ProviderError("После удаления список IPv4 1Cent изменился неоднозначно")
            raise ProviderError("1Cent ещё показывает удаляемый IPv4")

    @staticmethod
    def record_release(state, owned, ip, journal):
        pending = state["releases"].get(ip)
        if isinstance(pending, dict) and "idempotency_key" in pending:
            # Keep the observed refund and stable dispatch key even after the
            # compatibility marker becomes the terminal string "released".
            state.setdefault("release_evidence", {})[pending["idempotency_key"]] = {
                **pending, "phase": "released", "completion_basis": "exact_expected_inventory",
            }
        state["releases"][ip] = "released"
        for entry in owned:
            entry["phase"] = "released"
        journal.save()

    def reconcile(self, request_id, target):
        request_id = self.request_id(request_id)
        with Journal(self.settings.journal) as journal:
            entry = self.orders(journal)["orders"].get(request_id)
            if entry is None and target is None:
                # A crash after reserving but before writing the order journal
                # cannot have clicked: the budget dispatch marker is absent.
                budget = self.account_budget()
                orphan = budget.record(self.server_id, request_id)
                if orphan is not None and orphan["phase"] == "reserved":
                    budget.transition(self.server_id, request_id, "no_purchase")
                    return
            if not entry or entry.get("request_id") != request_id or entry["phase"] != "submitting":
                raise ProviderError("Нет неподтверждённого заказа 1Cent с таким ID")
            if target is None:
                budget = self.entry_budget(entry)
                record = budget.record(self.server_id, request_id) if budget else None
                if (record is None or record["phase"] != "reserved"
                        or any(name in entry for name in ("receipt", "paid_response", "ownership"))):
                    raise ProviderError("Неизвестный результат платного заказа 1Cent нельзя отменить по списку IPv4")
                budget.transition(self.server_id, request_id, "no_purchase")
                entry["phase"] = "cancelled"
            else:
                proof = self.entry_ownership(entry)
                if target.ref != target.ip or target.ip != proof["ip"]:
                    raise ProviderError("Указанный IPv4 не соответствует платному ответу 1Cent")
                self.wait_for_purchase(entry["before"], proof["ip"])
                budget = self.entry_budget(entry)
                if budget:
                    budget.transition(self.server_id, request_id, "confirmed")
                entry.update(phase="confirmed", ip=target.ip)
            journal.save()

    @staticmethod
    def request_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            raise ValueError("Неверный ID запроса заказа")
        return value


@contextmanager
def browser(settings, *, login=False):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        options = {"channel": settings.browser_channel} if settings.browser_channel else {}
        context = playwright.chromium.launch_persistent_context(
            str(private_profile(settings.profile)), headless=not login, accept_downloads=False,
            service_workers="block", **options,
        )
        try:
            yield context.pages[0] if context.pages else context.new_page()
        finally:
            context.close()


def execute(billing, request):
    base = {"operation", "server_id"}
    if request["server_id"] != billing.server_id:
        raise ValueError("Запрос относится к другой услуге 1Cent")
    operation = request["operation"]
    if request.keys() == base:
        if operation == "inventory":
            return {"allocations": [Allocation(ip, ip).as_payload() for ip in billing.inventory()]}
        if operation in ("check", "quote"):
            if operation == "check":
                billing.require_ownership_policy()
            billing.inventory()
            quote = billing.quote()
            billing.ui.close_quote()
            if operation == "check":
                billing.account_budget(for_purchase=True).check(quote.upfront_rub)
                return {"ok": True}
            return {"quantity": 1, "upfront_rub": str(quote.upfront_rub), "monthly_rub": str(quote.monthly_rub)}
    if operation == "allocate" and request.keys() == base | {"request_id"}:
        return billing.allocate(request["request_id"]).as_payload()
    if operation == "release" and request.keys() == base | {"id", "ip"}:
        billing.release(Allocation(request["id"], request["ip"]))
        return {"ok": True}
    if operation == "retire" and request.keys() == base | {"old", "current"}:
        billing.release(Allocation.from_payload(request["old"]), replacement=Allocation.from_payload(request["current"]))
        return {"ok": True}
    if operation == "reconcile" and request.keys() == base | {"request_id", "allocation"}:
        result = request["allocation"]
        billing.reconcile(request["request_id"], Allocation.from_payload(result) if result else None)
        return {"ok": True}
    raise ValueError("Неизвестная команда адаптера 1Cent")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.getenv("ONECENT_CONFIG"))
    parser.add_argument("mode", nargs="?", choices=("login",))
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(args.config)
        if args.mode == "login":
            with browser(settings, login=True) as page:
                page.goto(ORIGIN + "/dashboard/services", timeout=20000)
                input("Войди в 1Cent в отдельном окне. Когда список услуг откроется, нажми Enter. ")
            return 0
        raw = sys.stdin.buffer.read(4097)
        if not 0 < len(raw) <= 4096:
            raise ValueError("Команда 1Cent отсутствует или слишком велика")
        request = json.loads(raw, object_pairs_hook=_unique_object)
        uid = normalize_service_id(request["server_id"])
        from .onecent_ui import OneCentPage
        with browser(settings) as page:
            email, password = os.getenv("ONECENT_EMAIL"), os.getenv("ONECENT_PASSWORD")
            login = None
            if email or password:
                login = OneCentLogin(page, email, password, settings.journal / "authentication")
            ui = OneCentPage(page, uid, settings.servers[uid].name, login=login)
            ui.open()
            response = execute(OneCentBilling(settings, uid, ui), request)
        print(json.dumps(response))
        return 0
    except ImportError:
        print("Для адаптера 1Cent нужен Playwright Chromium.", file=sys.stderr)
    except BudgetDenied:
        # Machine-readable, definite no-dispatch result, unlike an unknown UI
        # response. The controller must not treat a budget stop as a purchase.
        print(json.dumps({"error": "not_submitted", "reason": "budget_limit"}))
        return 0
    except (ProviderError, OneCentLoginError) as exc:
        print(str(exc), file=sys.stderr)
    except (Exception, KeyboardInterrupt):
        print("Операция 1Cent не подтверждена. Проверь профиль и состояние заказа в кабинете.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
