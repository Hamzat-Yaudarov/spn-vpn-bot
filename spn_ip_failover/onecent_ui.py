"""Read and operate a scoped IPv4 section in the 1Cent billing interface."""

from decimal import Decimal
import hashlib
from ipaddress import IPv4Address
import json
import re
import time
from urllib.parse import urlsplit
from uuid import UUID

from .onecent_fields import Address, Quote, ROW, normalize_service_id, parse_api_addresses, parse_pool_variant, parse_quote, parse_rows
from .provider import _unique_object

try:
    from playwright.sync_api import Error as _PlaywrightReadError
except ImportError:
    # Pure parser/unit-test use must not require the optional browser runtime.
    class _PlaywrightReadError(Exception):
        pass


ORIGIN = "https://bill.1cent.host"
ORDER_TITLE = "Заказ IPv4-адресов"
CURRENT_ORDER_TITLE = "Дополнительный IPv4-адрес"
DELETE = re.compile(r"^Удалить адрес \(вернём ~[0-9]+(?:[.,][0-9]{2})? ₽ за неиспользованные дни\)$")
PAID_RESPONSE_LIMIT = 256 * 1024
PAID_RESPONSE_WAIT_MS = 20000


class OneCentUIError(RuntimeError):
    """The authenticated page or the requested action could not be confirmed."""


def _planned_key(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise OneCentUIError("Для заказа 1Cent нужен сохранённый UUID Idempotency-Key") from None
    return value


def _continue_with_key(route, key):
    request = route.request
    if request.header_value("idempotency-key") == key:
        route.continue_()
    else:
        # Normal credential headers are forwarded only in memory. Evidence
        # captures no request headers except the planned non-secret key.
        headers = {name: value for name, value in request.headers.items()
                   if name.lower() != "idempotency-key"}
        route.continue_(headers={**headers, "Idempotency-Key": key})


def normalize_paid_response(response, *, service_id, variant_id, request_id, idempotency_key, expected_upfront):
    """Normalize the observed paid response, which deliberately names no IP.

    The caller must first bind this response to its exact guarded Request.
    Only financial/dispatch evidence is returned; inventory ownership is a
    separate, explicitly exclusive-policy decision in the billing layer.
    """
    path = f"/api/services/{service_id}/ipv4/add"
    try:
        if (response.url != ORIGIN + path or response.request.url != ORIGIN + path
                or response.request.method != "POST"
                or response.request.post_data_json != {"variantId": variant_id}
                or type(response.status) is not int or response.status != 200):
            raise ValueError
        length = response.header_value("content-length")
        if length is not None and (not isinstance(length, str)
                or re.fullmatch(r"[0-9]{1,12}", length) is None or int(length) > PAID_RESPONSE_LIMIT):
            raise ValueError
        raw = response.body()
        if not isinstance(raw, bytes) or not 0 < len(raw) <= PAID_RESPONSE_LIMIT:
            raise ValueError
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(body, dict) or body.keys() != {"pending", "address", "amount"}
                or body["pending"] is not False or body["address"] is not None
                or not isinstance(body["amount"], str)
                or re.fullmatch(r"(?:0|[1-9]\d{0,7})\.\d{2}", body["amount"]) is None
                or not isinstance(expected_upfront, Decimal) or not expected_upfront.is_finite()
                or Decimal(body["amount"]) != expected_upfront):
            raise ValueError
        _planned_key(idempotency_key)
        if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id) is None:
            raise ValueError
    except Exception:
        raise OneCentUIError("Платный ответ 1Cent не подтвердил проверенную схему и сумму") from None
    return {"format": 1, "basis": "guarded_paid_post_v1", "request_id": request_id,
            "service_id": service_id, "variant_id": variant_id, "idempotency_key": idempotency_key,
            "method": "POST", "path": path, "http_status": 200, "pending": False, "address": None,
            "amount_rub": body["amount"], "response_sha256": hashlib.sha256(raw).hexdigest()}


def normalize_release_response(response, *, service_id, target, row_id, idempotency_key, expected_refund):
    """Accept only the observed, exact-target successful release response."""
    path = f"/api/services/{service_id}/ipv4/{row_id}"
    try:
        if (response.url != ORIGIN + path or response.request.url != ORIGIN + path
                or response.request.method != "DELETE" or response.request.post_data is not None
                or type(response.status) is not int or response.status != 200):
            raise ValueError
        length = response.header_value("content-length")
        if length is not None and (not isinstance(length, str)
                or re.fullmatch(r"[0-9]{1,12}", length) is None or int(length) > PAID_RESPONSE_LIMIT):
            raise ValueError
        raw = response.body()
        if not isinstance(raw, bytes) or not 0 < len(raw) <= PAID_RESPONSE_LIMIT:
            raise ValueError
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(body, dict) or body.keys() != {"released", "refunded"}
                or body["released"] != target or not isinstance(body["refunded"], str)
                or re.fullmatch(r"(?:0|[1-9]\d{0,7})\.\d{2}", body["refunded"]) is None
                or Decimal(body["refunded"]) != expected_refund):
            raise ValueError
        _planned_key(idempotency_key)
    except Exception:
        raise OneCentUIError("Удаление IPv4 1Cent не подтвердило точный адрес и возврат") from None
    return {"format": 1, "basis": "guarded_release_v1", "service_id": service_id,
            "idempotency_key": idempotency_key, "method": "DELETE", "path": path, "http_status": 200,
            "released": target, "refunded_rub": body["refunded"], "response_sha256": hashlib.sha256(raw).hexdigest()}


def _one(locator):
    return locator.count() == 1 and locator.is_visible()


def _ancestor(anchor, matches, error):
    current = anchor
    for _ in range(14):
        current = current.locator("xpath=..")
        if current.count() != 1:
            break
        if matches(current):
            return current
    raise OneCentUIError(error)


def _text(locator, name):
    return locator.get_by_text(name, exact=True)


def _button(locator, name):
    return locator.get_by_role("button", name=name, exact=True)


def _heading(locator, name):
    return locator.get_by_role("heading", name=name, exact=True)


class OneCentPage:
    def __init__(self, page, server_id: str, expected_name: str, *, login=None):
        self.page = page
        self.server_id = normalize_service_id(server_id)
        if not isinstance(expected_name, str) or not 1 <= len(expected_name.strip()) <= 150:
            raise ValueError("Нужно точное название услуги 1Cent")
        self.expected_name = expected_name.strip()
        if login is not None and not callable(login):
            raise ValueError("Для входа 1Cent требуется функция авторизации")
        self._login = login
        self.path = f"/dashboard/services/{self.server_id}"
        self._opened_quote = None
        self._quote_kind = None
        self._quote_variant = None
        self._guard = None
        self._allow_payment = False
        self._payment_sent = False
        self._planned_idempotency_key = None
        self._paid_request = None
        self._guarded_payment_key = None
        self._payment_guard_error = False
        self._release_guard = None

    def _install_quote_guard(self):
        if self._guard is not None:
            raise OneCentUIError("Защитная проверка заказа 1Cent уже установлена")
        self._allow_payment = False
        self._payment_sent = False
        self._planned_idempotency_key = None
        self._paid_request = None
        self._guarded_payment_key = None
        self._payment_guard_error = False

        def guard(route):
            request = route.request
            location = urlsplit(request.url)
            same_origin = (
                location.scheme == "https" and location.hostname == "bill.1cent.host"
                and location.port in (None, 443) and location.username is None
                and location.password is None and not location.query and not location.fragment
            )
            if request.method in ("GET", "HEAD", "OPTIONS"):
                route.continue_()
                return
            # This is an observed read-only price calculator, not the order RPC.
            if (request.method == "POST" and same_origin
                    and location.path == f"/api/services/{self.server_id}/change-preview"
                    and not self._allow_payment):
                route.continue_()
                return
            if (request.method == "POST" and same_origin and self._allow_payment
                    and not self._payment_sent and self._quote_variant is not None
                    and location.path == f"/api/services/{self.server_id}/ipv4/add"):
                try:
                    payload = request.post_data_json
                except Exception:
                    payload = None
                if payload == {"variantId": self._quote_variant.id}:
                    # Consume before forwarding, including header/transport
                    # failures. A stable key never grants a second attempt.
                    self._payment_sent = True
                    self._paid_request = request
                    try:
                        key = _planned_key(self._planned_idempotency_key)
                        _continue_with_key(route, key)
                        self._guarded_payment_key = key
                    except Exception:
                        self._payment_guard_error = True
                        try:
                            route.abort()
                        except Exception:
                            pass
                    return
            route.abort()

        self._guard = guard
        self.page.route("**/*", guard)

    def _remove_quote_guard(self):
        if self._guard is not None:
            self.page.unroute("**/*", self._guard)
            self._guard = None
        self._allow_payment = False

    def _assert_url(self):
        location = urlsplit(self.page.url)
        if (
            location.scheme != "https" or location.hostname != "bill.1cent.host"
            or location.port not in (None, 443) or location.path != self.path
            or location.username is not None or location.password is not None
        ):
            raise OneCentUIError("Открыт другой кабинет или другая услуга 1Cent")

    def _assert_service(self):
        self._assert_url()
        if not _one(_heading(self.page, self.expected_name)):
            raise OneCentUIError("На странице 1Cent не подтверждено точное название услуги")

    def open(self):
        self._opened_quote = None
        self._quote_kind = None
        self._quote_variant = None
        self._allow_payment = False
        response = self.page.goto(ORIGIN + self.path, wait_until="domcontentloaded", timeout=20_000)
        self._remove_quote_guard()
        if self._release_guard is not None:
            self.page.unroute("**/*", self._release_guard)
            self._release_guard = None
        if response is None or not response.ok:
            raise OneCentUIError("Кабинет 1Cent не вернул страницу выбранной услуги")
        if self._login is not None:
            _heading(self.page, self.expected_name).or_(_heading(self.page, "Вход в кабинет")).wait_for(
                state="visible", timeout=12_000,
            )
        location = urlsplit(self.page.url)
        if (
            self._login is not None and location.scheme == "https" and location.hostname == "bill.1cent.host"
            and location.port in (None, 443) and location.path == "/login"
            and location.username is None and location.password is None
        ):
            self._login()
            response = self.page.goto(ORIGIN + self.path, wait_until="domcontentloaded", timeout=20_000)
            if response is None or not response.ok:
                raise OneCentUIError("Кабинет 1Cent не вернул страницу выбранной услуги после входа")
        self._assert_url()
        _heading(self.page, self.expected_name).wait_for(state="visible", timeout=12_000)
        self._assert_service()

    def _ipv4_section(self):
        self._assert_service()
        heading = _heading(self.page, "IPv4")
        heading.wait_for(state="visible", timeout=10_000)
        _button(self.page, "Заказать IPv4").wait_for(state="visible", timeout=10_000)
        if not _one(heading):
            raise OneCentUIError("На странице 1Cent не подтверждён раздел IPv4")
        return _ancestor(
            heading,
            lambda node: _one(_button(node, "Заказать IPv4")) and _one(_heading(node, "IPv4"))
            and _heading(node, "IPv6").count() == 0 and _text(node, "Тип").count() > 0,
            "Не удалось отделить раздел IPv4 выбранной услуги от других разделов",
        )

    def _rows(self, section):
        deadline = time.monotonic() + 8
        previous = None
        while True:
            try:
                self._assert_service()
                rows = self._read_rows(section)
                current = tuple(address for _, address in rows)
                if current == previous:
                    return rows
                previous = current
            except (OneCentUIError, ValueError, _PlaywrightReadError):
                # React can replace rows while polling the service. Only this
                # bounded, read-only snapshot loop retries browser detachment/
                # timeout errors; order/delete clicks are never retried here.
                previous = None
            if time.monotonic() >= deadline:
                raise OneCentUIError("Таблица IPv4 1Cent не загрузилась в подтверждённом виде")
            time.sleep(0.2)

    def _read_rows(self, section):
        rows = []
        for label in _text(section, "Тип").all():
            if not label.is_visible():
                raise OneCentUIError("В разделе IPv4 есть скрытая строка адреса")
            row = _ancestor(
                label,
                lambda node: _one(_text(node, "Тип")) and _one(_text(node, "Адрес"))
                and _one(_text(node, "PTR (домен)")) and _heading(node, "IPv4").count() == 0,
                "Строка адреса 1Cent не совпадает с наблюдаемой структурой",
            )
            text = " ".join(row.inner_text(timeout=3_000).split())
            fields = ROW.match(text)
            if fields is None or not _one(_text(row, fields["ip"])):
                raise OneCentUIError("Не удалось подтвердить IP в конкретной строке 1Cent")
            rows.append((row, fields[0]))
        addresses = parse_rows([fields for _, fields in rows])
        return [(row, address) for (row, _), address in zip(rows, addresses)]

    def inventory(self) -> list[Address]:
        # Read the observed API snapshot, not React elements replaced by page
        # polling. Both reads still require the exact UI heading and service URL.
        # The billing API can take several seconds per read. Keep two complete
        # matching snapshots, but do not give the whole pair only one 8s slot.
        # This retries reads only, never an order or deletion.
        deadline = time.monotonic() + 30
        previous = None
        while True:
            try:
                remaining = max(1, int((deadline - time.monotonic()) * 1000))
                current = self._api_inventory(timeout=min(10000, remaining))
                if current == previous:
                    return current
                previous = current
            except (OneCentUIError, ValueError, _PlaywrightReadError):
                previous = None
            if time.monotonic() >= deadline:
                raise OneCentUIError("API 1Cent не подтвердил два стабильных снимка IPv4")
            time.sleep(0.5)

    def _api_address_snapshot(self, *, timeout):
        self._assert_service()
        url = f"{ORIGIN}/api/services/{self.server_id}/ipv4/addresses"
        response = self.page.context.request.get(
            url, timeout=timeout, max_redirects=0,
            headers={"Accept": "application/json", "Cache-Control": "no-cache"},
        )
        try:
            if response.status != 200 or response.url != url:
                raise OneCentUIError("API 1Cent не вернул выбранную услугу без перенаправления")
            raw = response.body()
            if not 0 < len(raw) <= 1_000_000:
                raise OneCentUIError("Ответ IPv4 1Cent превышает допустимый размер")
            snapshot = json.loads(raw, object_pairs_hook=_unique_object)
            parse_api_addresses(snapshot)
            self._assert_service()
            return snapshot
        finally:
            response.dispose()

    def _api_inventory(self, *, timeout) -> list[Address]:
        return parse_api_addresses(self._api_address_snapshot(timeout=timeout))

    def _quote_panel(self):
        self._assert_service()
        old_heading = _heading(self.page, ORDER_TITLE)
        current_heading = _heading(self.page, CURRENT_ORDER_TITLE)
        old_heading.or_(current_heading).wait_for(state="visible", timeout=8_000)
        if _one(current_heading):
            if old_heading.count():
                raise OneCentUIError("В кабинете 1Cent открыты неоднозначные формы заказа")
            panel = self.page.get_by_role("dialog").filter(has=current_heading)
            if not _one(panel) or not _one(_button(panel, "Отмена")):
                raise OneCentUIError("Не подтверждена отдельная форма дополнительного IPv4 1Cent")
            self._quote_kind = "current"
            return panel
        heading = _heading(self.page, ORDER_TITLE)
        heading.wait_for(state="visible", timeout=8_000)
        if not _one(heading):
            raise OneCentUIError("Форма заказа IPv4 1Cent не подтверждена")
        self._quote_kind = "legacy"
        return _ancestor(
            heading,
            lambda node: _one(_button(node, "Заказать")) and _one(_button(node, "Закрыть"))
            and _one(_text(node, "Количество адресов")) and _heading(node, "IPv4").count() == 0,
            "Не удалось выделить отдельную форму заказа IPv4 1Cent",
        )

    def _read_quote(self, panel) -> Quote:
        if self._quote_kind == "current":
            return self._read_current_quote(panel)
        minus, plus = _button(panel, "-"), _button(panel, "+")
        if not _one(minus) or not _one(plus):
            raise OneCentUIError("Форма 1Cent не показала известные элементы количества")
        counter = _ancestor(
            minus,
            lambda node: _one(_button(node, "+")) and _one(_button(node, "-"))
            and _heading(node, ORDER_TITLE).count() == 0,
            "Не удалось выделить счётчик IPv4 в форме 1Cent",
        )
        number = _text(counter, re.compile(r"^[0-9]+$"))
        deadline = time.monotonic() + 8
        while True:
            if not _one(number) or number.inner_text(timeout=3_000).strip() != "1":
                raise OneCentUIError("Форма 1Cent должна заказывать ровно один IPv4")
            try:
                return parse_quote(panel.inner_text(timeout=3_000))
            except ValueError:
                if time.monotonic() >= deadline:
                    raise OneCentUIError("Кабинет 1Cent не показал окончательную цену IPv4") from None
                time.sleep(0.2)

    def _load_current_variant(self):
        payloads = []
        for suffix in ("ipv4/addresses", "ipv4"):
            response = self.page.request.get(
                f"{ORIGIN}/api/services/{self.server_id}/{suffix}", timeout=10_000,
                max_redirects=0,
            )
            if not response.ok:
                raise OneCentUIError("1Cent не подтвердил параметры обычного дополнительного IPv4")
            try:
                payloads.append(response.json())
            except Exception:
                raise OneCentUIError("1Cent не вернул параметры обычного IPv4") from None
        try:
            return parse_pool_variant(*payloads)
        except ValueError as exc:
            raise OneCentUIError(str(exc)) from None

    def _assert_selected_variant(self, panel):
        variant = self._quote_variant
        if variant is None:
            raise OneCentUIError("Вариант дополнительного IPv4 1Cent не подтверждён")
        radios = panel.get_by_role("radio").all()
        checked = [radio for radio in radios if radio.get_attribute("aria-checked") == "true"]
        expected = f"{variant.name} {variant.monthly_rub:.2f} ₽ в месяц".replace(".", ",")
        if (len(checked) != 1 or not checked[0].is_visible()
                or " ".join(checked[0].inner_text(timeout=3_000).split()) != expected):
            raise OneCentUIError("Выбран не обычный дополнительный IPv4 из пула 1Cent")

    def _wait_selected_variant(self, panel):
        deadline = time.monotonic() + 8
        while True:
            try:
                self._assert_selected_variant(panel)
                return
            except OneCentUIError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    def _read_current_quote(self, panel):
        self._assert_selected_variant(panel)
        if not _one(_button(panel, "Да, добавить")) or _button(panel, "Добавить").count():
            raise OneCentUIError("Окончательный шаг заказа IPv4 1Cent не подтверждён")
        deadline = time.monotonic() + 8
        while True:
            try:
                if not _button(panel, "Да, добавить").is_enabled():
                    raise ValueError("Цена ещё рассчитывается")
                quote = parse_quote(panel.inner_text(timeout=3_000))
                if quote.monthly_rub != self._quote_variant.monthly_rub:
                    raise OneCentUIError("Цена IPv4 в форме и параметрах 1Cent не совпадает")
                return quote
            except ValueError:
                if time.monotonic() >= deadline:
                    raise OneCentUIError("Кабинет 1Cent не показал окончательную цену IPv4") from None
                time.sleep(0.2)

    def open_quote(self) -> Quote:
        section = self._ipv4_section()
        if (self._opened_quote is not None or _heading(self.page, ORDER_TITLE).count()
                or _heading(self.page, CURRENT_ORDER_TITLE).count()):
            raise OneCentUIError("В кабинете 1Cent уже открыта форма заказа")
        self._assert_service()
        self._install_quote_guard()
        try:
            self._quote_variant = self._load_current_variant()
            deadline = time.monotonic() + 8
            while True:
                _button(section, "Заказать IPv4").click(timeout=8_000)
                panel = self._quote_panel()
                if self._quote_kind == "current":
                    break
                # During hydration the current UI can briefly open its obsolete
                # quantity form before orderFlow.enabled arrives. Never submit it.
                _button(panel, "Закрыть").click(timeout=8_000)
                _heading(self.page, ORDER_TITLE).wait_for(state="hidden", timeout=8_000)
                if time.monotonic() >= deadline:
                    raise OneCentUIError("1Cent не загрузил подтверждённую форму IPv4 из пула")
                time.sleep(0.2)
            self._wait_selected_variant(panel)
            initial = _button(panel, "Добавить")
            if not _one(initial) or not initial.is_enabled() or _button(panel, "Да, добавить").count():
                raise OneCentUIError("Первый шаг формы IPv4 1Cent не подтверждён")
            # Only opens the final confirmation; guard forbids every paid RPC.
            initial.click(timeout=8_000)
            _button(panel, "Да, добавить").wait_for(state="visible", timeout=8_000)
            quote = self._read_quote(panel)
            self._opened_quote = quote
            return quote
        except Exception:
            self._remove_quote_guard()
            self._quote_variant = None
            raise

    def order_variant_id(self):
        if self._quote_kind != "current" or self._quote_variant is None:
            raise OneCentUIError("Вариант дополнительного IPv4 не подтверждён")
        return self._quote_variant.id

    def submit_order(self, expected_quote: Quote, *, request_id=None, idempotency_key=None):
        key = _planned_key(idempotency_key)
        if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id) is None:
            raise OneCentUIError("Нужен точный сохранённый ID запроса 1Cent")
        if not isinstance(expected_quote, Quote) or expected_quote != self._opened_quote:
            raise OneCentUIError("Цена заказа 1Cent не совпадает с подтверждённой формой")
        panel = self._quote_panel()
        if self._read_quote(panel) != expected_quote:
            raise OneCentUIError("Цена заказа 1Cent изменилась")
        if (self._quote_kind != "current" or self._guard is None or self._payment_sent
                or self._load_current_variant() != self._quote_variant):
            raise OneCentUIError("Параметры или защита заказа IPv4 1Cent изменились")
        if self._read_quote(panel) != expected_quote:
            raise OneCentUIError("Цена заказа 1Cent изменилась")
        variant_id = self.order_variant_id()
        button = _button(panel, "Да, добавить")
        if not _one(button) or not button.is_enabled():
            raise OneCentUIError("Кнопка заказа IPv4 1Cent недоступна")
        self._assert_service()
        proofs, seen_responses = [], []
        capture_failed = False

        def matches(response):
            try:
                return (self._paid_request is not None and response.request is self._paid_request
                        and response.url == ORIGIN + f"/api/services/{self.server_id}/ipv4/add"
                        and response.request.url == response.url and response.request.method == "POST"
                        and response.request.post_data_json == {"variantId": variant_id})
            except Exception:
                return False

        def observed(response):
            nonlocal capture_failed
            if not matches(response) or any(item is response for item in seen_responses):
                return
            seen_responses.append(response)
            try:
                if len(seen_responses) != 1:
                    raise ValueError
                proofs.append(normalize_paid_response(
                    response, service_id=self.server_id, variant_id=variant_id, request_id=request_id,
                    idempotency_key=key, expected_upfront=expected_quote.upfront_rub,
                ))
            except Exception:
                # A failed callback must hold the outcome instead of letting a
                # successful-looking UI or inventory delta authorize ownership.
                capture_failed = True

        self.page.on("response", observed)
        self._opened_quote = None
        self._planned_idempotency_key = key
        self._allow_payment = True
        try:
            try:
                button.click(timeout=8_000)
                _heading(self.page, CURRENT_ORDER_TITLE).wait_for(state="hidden", timeout=15_000)
            except Exception:
                # A click/UI timeout can follow the sole successful POST. Pump
                # one bounded late response wait, never another click.
                pass
        finally:
            # Keep blocking any late client-side retry until navigation/closure.
            self._allow_payment = False
            try:
                if self._payment_sent and not seen_responses and not capture_failed:
                    try:
                        late = self.page.wait_for_event("response", predicate=matches, timeout=PAID_RESPONSE_WAIT_MS)
                        if not matches(late):
                            raise ValueError
                        observed(late)
                    except Exception:
                        capture_failed = True
            finally:
                try:
                    self.page.remove_listener("response", observed)
                except Exception:
                    capture_failed = True
        if (capture_failed or self._payment_guard_error or not self._payment_sent
                or self._guarded_payment_key != key or len(proofs) != 1):
            raise OneCentUIError("Результат единственного платного запроса 1Cent не подтверждён; не повторять")
        return proofs[0]

    def close_quote(self):
        try:
            panel = self._quote_panel()
            _button(panel, "Отмена" if self._quote_kind == "current" else "Закрыть").click(timeout=8_000)
            _heading(self.page, CURRENT_ORDER_TITLE if self._quote_kind == "current" else ORDER_TITLE).wait_for(
                state="hidden", timeout=8_000,
            )
        finally:
            self._opened_quote = None
            self._quote_variant = None
            self._remove_quote_guard()

    def remove_additional(self, address: str, *, before_submit=None, idempotency_key=None, expected_inventory=None):
        target = str(IPv4Address(address))
        section = self._ipv4_section()
        candidates = [entry for _, entry in self._rows(section) if entry.ip == target and not entry.primary]
        if len(candidates) != 1:
            raise OneCentUIError("Не подтверждён дополнительный IPv4 для удаления")
        return self._remove_address(section, target, "Дополнительный", before_submit=before_submit,
                                    idempotency_key=idempotency_key, expected_inventory=expected_inventory)

    def remove_primary(self, address: str, replacement: str, *, before_submit=None,
                       idempotency_key=None, expected_inventory=None):
        target = str(IPv4Address(address))
        new_address = str(IPv4Address(replacement))
        section = self._ipv4_section()
        entries = [entry for _, entry in self._rows(section)]
        if (
            target == new_address or len(entries) != 2
            or Address(target, primary=True) not in entries
            or Address(new_address, primary=False) not in entries
        ):
            raise OneCentUIError("Для удаления главного IPv4 1Cent нужны ровно старый главный и новый дополнительный")
        return self._remove_address(section, target, "Главный", before_submit=before_submit,
                                    idempotency_key=idempotency_key, replacement=new_address,
                                    expected_inventory=expected_inventory)

    def _remove_address(self, section, target, address_type, *, before_submit=None,
                        idempotency_key=None, replacement=None, expected_inventory=None):
        key = _planned_key(idempotency_key)
        if (not callable(before_submit) or self._release_guard is not None
                or not isinstance(expected_inventory, dict)
                or any(type(flag) is not bool for flag in expected_inventory.values())):
            raise OneCentUIError("Удаление 1Cent требует нового сохранённого одноразового запроса")
        address_label = _text(section, target)
        if not _one(address_label):
            raise OneCentUIError("На странице 1Cent изменился выбранный IPv4")
        row = _ancestor(
            address_label,
            lambda node: _one(_button(node, DELETE)) and _one(_text(node, "Тип"))
            and _one(_text(node, address_type)) and _heading(node, "IPv4").count() == 0,
            "Не удалось выделить действие удаления внутри одной строки 1Cent",
        ).filter(has=_text(self.page, address_type))
        button = _button(row, DELETE)
        if not _one(button) or not button.is_enabled():
            raise OneCentUIError("У выбранной строки IPv4 нет известной кнопки удаления")
        dialog = self.page.get_by_role("dialog").or_(self.page.get_by_role("alertdialog"))
        if dialog.count() or _heading(self.page, ORDER_TITLE).count():
            raise OneCentUIError("Перед удалением IPv4 открыта другая форма")
        self._assert_service()
        # The path component is an observed integer provider row index, never
        # the IP string or a guessed allocation identity. Bind it freshly twice
        # to the protected current roles before allowing the sole DELETE.
        first_snapshot = self._api_address_snapshot(timeout=10_000)
        second_snapshot = self._api_address_snapshot(timeout=10_000)
        parse_api_addresses(first_snapshot)
        parse_api_addresses(second_snapshot)
        first, second = first_snapshot["addresses"], second_snapshot["addresses"]
        if (first != second or {entry["address"]: entry["main"] for entry in second} != expected_inventory):
            raise OneCentUIError("Список IPv4 1Cent изменился перед удалением")
        ids = [entry["id"] for entry in second]
        if (any(type(item) is not int or not 0 <= item < 16 for item in ids)
                or len(set(ids)) != len(ids)):
            raise OneCentUIError("Не подтверждены целочисленные индексы IPv4 1Cent")
        targets = [entry for entry in second if entry["address"] == target]
        if (len(targets) != 1 or targets[0]["removable"] is not True
                or targets[0]["main"] is not (address_type == "Главный")):
            raise OneCentUIError("Роль выбранного IPv4 1Cent изменилась")
        if address_type == "Главный" and ({entry["address"]: entry["main"] for entry in second}
                                         != {target: True, replacement: False}):
            raise OneCentUIError("Перед удалением главного IPv4 изменилась проверенная замена")
        title = button.get_attribute("title")
        match = re.fullmatch(r"Удалить адрес \(вернём ~([0-9]+(?:[.,][0-9]{2})?) ₽ за неиспользованные дни\)",
                             title) if isinstance(title, str) else None
        if match is None:
            raise OneCentUIError("Не подтверждена точная сумма в кнопке удаления IPv4")
        refund = Decimal(match[1].replace(",", "."))
        expected_prompt = (f"Удалить адрес {target}? Он вернётся в общий пул, сайты и почта на нём перестанут работать. "
                           f"На баланс вернётся примерно {refund:.2f} ₽ за неиспользованные дни.")
        row_id = targets[0]["id"]
        path = f"/api/services/{self.server_id}/ipv4/{row_id}"
        url = ORIGIN + path
        consumed_request = None
        sent = confirmed = capture_failed = guard_failed = allow_release = False
        proofs, seen_responses = [], []

        def release_guard(route):
            nonlocal sent, consumed_request, guard_failed
            request = route.request
            if request.method in ("GET", "HEAD", "OPTIONS"):
                route.continue_()
                return
            if (allow_release and confirmed and not sent and request.method == "DELETE"
                    and request.url == url and request.post_data is None):
                sent = True  # Consume before any forwarding, even on failure.
                consumed_request = request
                try:
                    _continue_with_key(route, key)
                except Exception:
                    guard_failed = True
                    try:
                        route.abort()
                    except Exception:
                        pass
                return
            route.abort()

        def confirm(prompt):
            nonlocal confirmed, capture_failed
            try:
                if not confirmed and prompt.type == "confirm" and prompt.message == expected_prompt:
                    confirmed = True
                    prompt.accept()
                else:
                    capture_failed = True
                    prompt.dismiss()
            except Exception:
                capture_failed = True

        def matches(response):
            try:
                return consumed_request is not None and response.request is consumed_request and response.url == url
            except Exception:
                return False

        def observed(response):
            nonlocal capture_failed
            if not matches(response) or any(item is response for item in seen_responses):
                return
            seen_responses.append(response)
            try:
                if len(seen_responses) != 1:
                    raise ValueError
                proofs.append(normalize_release_response(
                    response, service_id=self.server_id, target=target, row_id=row_id,
                    idempotency_key=key, expected_refund=refund,
                ))
            except Exception:
                capture_failed = True

        self._release_guard = release_guard
        self.page.route("**/*", release_guard)
        self.page.on("dialog", confirm)
        self.page.on("response", observed)
        try:
            before_submit()  # Durable intent/key must precede every click.
            allow_release = True
            try:
                button.click(timeout=8_000)
                if dialog.count():
                    capture_failed = True  # Never confirm an unknown custom UI.
            except Exception:
                pass
        finally:
            allow_release = False
            try:
                if sent and not seen_responses and not capture_failed:
                    try:
                        late = self.page.wait_for_event("response", predicate=matches, timeout=PAID_RESPONSE_WAIT_MS)
                        if not matches(late):
                            raise ValueError
                        observed(late)
                    except Exception:
                        capture_failed = True
            finally:
                try:
                    self.page.remove_listener("dialog", confirm)
                    self.page.remove_listener("response", observed)
                except Exception:
                    capture_failed = True
        # Keep the one-shot guard installed: a late client retry can neither
        # mutate another target nor repeat a deletion with an unknown outcome.
        if capture_failed or guard_failed or not sent or not confirmed or len(proofs) != 1:
            raise OneCentUIError("Удаление IPv4 1Cent не подтверждено; повторного запроса не будет")
        return proofs[0]
