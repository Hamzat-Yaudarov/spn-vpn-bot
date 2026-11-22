from decimal import Decimal
from copy import deepcopy
import hashlib
from unittest import TestCase
from unittest.mock import Mock, patch

from spn_ip_failover.onecent_fields import Address, PoolVariant, Quote
from spn_ip_failover.onecent_ui import OneCentPage, OneCentUIError
from spn_ip_failover import onecent_ui


SERVER = "19a2f3c4-45d7-4a2e-8e31-aa8210b940a3"
REQUEST = "one-guarded-request"
KEY = "06f48090-cd6b-45f3-b3c9-58a4ed7ff95c"


def locator(count=1):
    mock = Mock()
    mock.count.return_value = count
    mock.is_visible.return_value = True
    mock.is_enabled.return_value = True
    return mock


class OneCentUITest(TestCase):
    def setUp(self):
        self.page = Mock()
        self.page.url = f"https://bill.1cent.host/dashboard/services/{SERVER}"
        self.service = OneCentPage(self.page, SERVER, "VPS demo")
        self.quote = Quote(Decimal("12.90"), Decimal("30.00"))

    def request_route(self, method, path, payload=None, origin="https://bill.1cent.host"):
        route = Mock()
        route.request.method = method
        route.request.url = origin + path
        route.request.post_data_json = payload
        route.request.header_value.return_value = KEY
        route.request.headers = {"content-type": "application/json", "idempotency-key": KEY}
        return route

    def paid_response(self, request=None, *, raw=b'{"pending":false,"address":null,"amount":"12.90"}', status=200):
        response = Mock()
        response.request = request or self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"}).request
        response.url = f"https://bill.1cent.host/api/services/{SERVER}/ipv4/add"
        response.status = status
        response.header_value.return_value = str(len(raw))
        response.body.return_value = raw
        return response

    def prepare_payment(self):
        self.service._install_quote_guard()
        self.service._quote_kind = "current"
        variant = PoolVariant("pool1", "Дополнительный адрес", self.quote.monthly_rub)
        self.service._quote_variant = variant
        self.service._opened_quote = self.quote
        return variant

    def submit(self, button):
        with (
            patch.object(self.service, "_quote_panel"),
            patch.object(self.service, "_read_quote", return_value=self.quote),
            patch.object(self.service, "_load_current_variant", return_value=self.service._quote_variant),
            patch.object(self.service, "_assert_service"),
            patch("spn_ip_failover.onecent_ui._button", return_value=button),
            patch("spn_ip_failover.onecent_ui._heading"),
        ):
            return self.service.submit_order(self.quote, request_id=REQUEST, idempotency_key=KEY)

    def normalize(self, response):
        return onecent_ui.normalize_paid_response(response, service_id=SERVER, variant_id="pool1",
                                                  request_id=REQUEST, idempotency_key=KEY,
                                                  expected_upfront=self.quote.upfront_rub)

    def release_snapshot(self):
        return {"operation": None, "orderFlow": {"order": None}, "addresses": [
            {"id": 0, "address": "8.8.8.8", "main": True, "ptr": None, "releasing": False, "removable": True},
            {"id": 1, "address": "1.1.1.1", "main": False, "ptr": None, "releasing": False, "removable": True},
            {"id": 2, "address": "9.9.9.9", "main": False, "ptr": None, "releasing": False, "removable": True},
        ]}

    def release_route(self, *, path=None, body=None):
        route = self.request_route("DELETE", path or f"/api/services/{SERVER}/ipv4/2")
        route.request.post_data = body
        return route

    def release_response(self, request, *, raw=b'{"released":"9.9.9.9","refunded":"12.34"}', status=200):
        response = Mock()
        response.request = request
        response.url = f"https://bill.1cent.host/api/services/{SERVER}/ipv4/2"
        response.status = status
        response.header_value.return_value = str(len(raw))
        response.body.return_value = raw
        return response

    def event_handler(self, event):
        return next(call.args[1] for call in reversed(self.page.on.call_args_list) if call.args[0] == event)

    def release_prompt(self):
        prompt = Mock()
        prompt.type = "confirm"
        prompt.message = ("Удалить адрес 9.9.9.9? Он вернётся в общий пул, сайты и почта на нём перестанут работать. "
                          "На баланс вернётся примерно 12.34 ₽ за неиспользованные дни.")
        return prompt

    def remove(self, button, *, snapshots=None, before_submit=None, expected_inventory=None):
        snapshot = self.release_snapshot()
        self.page.get_by_role.return_value.or_.return_value.count.return_value = 0
        button.get_attribute.return_value = "Удалить адрес (вернём ~12.34 ₽ за неиспользованные дни)"
        with (
            patch.object(self.service, "_ipv4_section"),
            patch.object(self.service, "_rows", return_value=[(Mock(), Address("9.9.9.9", False))]),
            patch.object(self.service, "_api_address_snapshot", side_effect=snapshots or [snapshot, snapshot]),
            patch.object(self.service, "_assert_service"),
            patch("spn_ip_failover.onecent_ui._ancestor"),
            patch("spn_ip_failover.onecent_ui._heading", return_value=locator(0)),
            patch("spn_ip_failover.onecent_ui._text", return_value=locator()),
            patch("spn_ip_failover.onecent_ui._button", return_value=button),
        ):
            return self.service.remove_additional(
                "9.9.9.9", before_submit=before_submit or Mock(), idempotency_key=KEY,
                expected_inventory=expected_inventory or {row["address"]: row["main"] for row in snapshot["addresses"]},
            )

    def test_api_inventory_accepts_two_complete_slow_matching_reads(self):
        rows = [Address("8.8.8.8", True)]
        with (patch.object(self.service, "_api_inventory", side_effect=[rows, rows]) as read,
              patch.object(onecent_ui.time, "monotonic", side_effect=[0, 1, 11, 12]),
              patch.object(onecent_ui.time, "sleep")):
            self.assertEqual(self.service.inventory(), rows)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args.kwargs["timeout"], 10000)

    def test_api_inventory_read_retries_still_have_a_deadline(self):
        with (patch.object(self.service, "_api_inventory", side_effect=OneCentUIError("read timeout")) as read,
              patch.object(onecent_ui.time, "monotonic", side_effect=[0, 0, 12, 13, 31]),
              patch.object(onecent_ui.time, "sleep") as sleep,
              self.assertRaises(OneCentUIError)):
            self.service.inventory()
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once()

    def test_read_only_inventory_row_detachment_retries_then_requires_two_fresh_stable_reads(self):
        rows = [(Mock(), Address("8.8.8.8", True))]
        with (patch.object(self.service, "_assert_service"),
              patch.object(self.service, "_read_rows", side_effect=[onecent_ui._PlaywrightReadError("detached"), rows, rows]) as read,
              patch("spn_ip_failover.onecent_ui.time.sleep") as sleep):
            self.assertEqual(self.service._rows(Mock()), rows)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.page.get_by_role.assert_not_called()

    def test_read_only_inventory_browser_retry_stays_bounded(self):
        with (patch.object(self.service, "_assert_service"),
              patch.object(self.service, "_read_rows", side_effect=onecent_ui._PlaywrightReadError("timeout")) as read,
              patch("spn_ip_failover.onecent_ui.time.monotonic", side_effect=[0, 1, 8.1]),
              patch("spn_ip_failover.onecent_ui.time.sleep") as sleep,
              self.assertRaises(OneCentUIError)):
            self.service._rows(Mock())
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once()

    def test_unrelated_inventory_programming_error_is_not_retried(self):
        with (patch.object(self.service, "_assert_service"),
              patch.object(self.service, "_read_rows", side_effect=RuntimeError("not a UI race")) as read,
              patch("spn_ip_failover.onecent_ui.time.sleep") as sleep,
              self.assertRaises(RuntimeError)):
            self.service._rows(Mock())
        read.assert_called_once()
        sleep.assert_not_called()

    def test_preparing_quote_blocks_every_write_except_exact_service_price_preview(self):
        self.service._install_quote_guard()
        preview = f"/api/services/{SERVER}/change-preview"
        for method, path, origin, allowed in (
            ("GET", "/static/main.js", "https://bill.1cent.host", True),
            ("POST", preview, "https://bill.1cent.host", True),
            ("POST", preview, "https://bill.1cent.host.evil", False),
            ("POST", preview, "http://bill.1cent.host", False),
            ("POST", preview + "?other=1", "https://bill.1cent.host", False),
            ("PUT", preview, "https://bill.1cent.host", False),
            ("POST", f"/api/services/{SERVER}/ipv4/add", "https://bill.1cent.host", False),
            ("DELETE", f"/api/services/{SERVER}/ipv4", "https://bill.1cent.host", False),
            ("POST", "/api/mkt/collect", "https://bill.1cent.host", False),
            ("POST", preview.replace(SERVER, "other"), "https://bill.1cent.host", False),
        ):
            route = self.request_route(method, path, origin=origin)
            self.service._guard(route)
            with self.subTest(method=method, path=path, origin=origin):
                self.assertEqual(route.continue_.call_count, int(allowed))
                self.assertEqual(route.abort.call_count, int(not allowed))

    def test_paid_permission_allows_exactly_one_exact_pool_post_and_nothing_else(self):
        self.service._install_quote_guard()
        self.service._quote_variant = PoolVariant("pool1", "Дополнительный адрес", Decimal("30.00"))
        self.service._allow_payment = True
        self.service._planned_idempotency_key = KEY
        path = f"/api/services/{SERVER}/ipv4/add"
        for payload in (None, {}, {"variantId": "chosen1"}, {"variantId": "pool1", "address": "8.8.8.8"},
                        {"variantId": "pool1", "quantity": 2}):
            route = self.request_route("POST", path, payload)
            self.service._guard(route)
            route.abort.assert_called_once()
            route.continue_.assert_not_called()
        for alternate in (path + "?other=1", path.replace(SERVER, "other"),
                          f"/api/services/{SERVER}/change-preview"):
            route = self.request_route("POST", alternate, {"variantId": "pool1"})
            self.service._guard(route)
            route.abort.assert_called_once()
        exact = self.request_route("POST", path, {"variantId": "pool1"})
        self.service._guard(exact)
        exact.continue_.assert_called_once()
        self.assertTrue(self.service._payment_sent)
        duplicate = self.request_route("POST", path, {"variantId": "pool1"})
        self.service._guard(duplicate)
        duplicate.abort.assert_called_once()

    def test_paid_guard_overrides_only_planned_key_and_consumes_before_dispatch(self):
        self.prepare_payment()
        self.service._allow_payment = True
        self.service._planned_idempotency_key = KEY
        route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        route.request.header_value.return_value = "browser-generated-other-key"
        route.request.headers = {"content-type": "application/json", "cookie": "private-fixture",
                                 "idempotency-key": "browser-generated-other-key"}
        observed = []
        def forwarded(**kwargs):
            observed.append((self.service._payment_sent, self.service._paid_request is route.request))
        route.continue_.side_effect = forwarded
        self.service._guard(route)
        self.assertEqual(observed, [(True, True)])
        self.assertEqual(route.continue_.call_args.kwargs["headers"],
                         {"content-type": "application/json", "cookie": "private-fixture", "Idempotency-Key": KEY})
        route.request.header_value.assert_called_once_with("idempotency-key")
        self.assertEqual(self.service._guarded_payment_key, KEY)

    def test_paid_guard_header_failure_aborts_and_never_reuses_permission(self):
        self.prepare_payment()
        self.service._allow_payment = True
        self.service._planned_idempotency_key = KEY
        route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        route.request.header_value.side_effect = RuntimeError("private header error")
        self.service._guard(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()
        self.assertTrue(self.service._payment_sent)
        self.assertTrue(self.service._payment_guard_error)
        duplicate = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        self.service._guard(duplicate)
        duplicate.abort.assert_called_once()

    def test_missing_or_noncanonical_planned_key_cannot_click(self):
        self.prepare_payment()
        for key in (None, "", "browser-key", KEY.upper()):
            with self.subTest(key=key), patch("spn_ip_failover.onecent_ui._button") as button, self.assertRaises(OneCentUIError):
                self.service.submit_order(self.quote, request_id=REQUEST, idempotency_key=key)
            button.assert_not_called()
        self.assertFalse(self.service._payment_sent)
        self.assertEqual(self.service._opened_quote, self.quote)

    def test_actual_null_address_response_has_financial_proof_but_no_provider_ip(self):
        raw = b'{"pending":false,"address":null,"amount":"248.39"}'
        response = self.paid_response(raw=raw)
        proof = onecent_ui.normalize_paid_response(response, service_id=SERVER, variant_id="pool1",
                                                  request_id=REQUEST, idempotency_key=KEY,
                                                  expected_upfront=Decimal("248.39"))
        self.assertEqual(proof, {"format": 1, "basis": "guarded_paid_post_v1", "request_id": REQUEST,
                                "service_id": SERVER, "variant_id": "pool1", "idempotency_key": KEY,
                                "method": "POST", "path": f"/api/services/{SERVER}/ipv4/add",
                                "http_status": 200, "pending": False, "address": None,
                                "amount_rub": "248.39", "response_sha256": hashlib.sha256(raw).hexdigest()})
        self.assertNotIn("ip", proof)
        response.header_value.assert_called_once_with("content-length")
        response.request.header_value.assert_not_called()

    def test_paid_response_rejects_pending_ip_amount_shape_and_duplicate_fields(self):
        for raw in (
            b'{"pending":true,"address":null,"amount":"12.90"}',
            b'{"pending":0,"address":null,"amount":"12.90"}',
            b'{"pending":false,"address":"9.9.9.9","amount":"12.90"}',
            b'{"pending":false,"address":null,"amount":12.90}',
            b'{"pending":false,"address":null,"amount":"12.9"}',
            b'{"pending":false,"address":null,"amount":"12.900"}',
            b'{"pending":false,"address":null,"amount":"13.00"}',
            b'{"pending":false,"address":null,"amount":"NaN"}',
            b'{"pending":false,"address":null,"amount":"12.90","ip":"9.9.9.9"}',
            b'{"pending":false,"pending":false,"address":null,"amount":"12.90"}',
            b'{"pending":false,"amount":"12.90"}', b'null', b'not-json',
        ):
            with self.subTest(raw=raw), self.assertRaises(OneCentUIError):
                self.normalize(self.paid_response(raw=raw))

    def test_paid_response_rejects_wrong_http_origin_service_method_or_variant_before_body(self):
        for mutate in (
            lambda r: setattr(r, "status", 201),
            lambda r: setattr(r, "status", True),
            lambda r: setattr(r, "url", r.url + "?other=1"),
            lambda r: setattr(r, "url", r.url.replace("bill.1cent.host", "foreign.invalid")),
            lambda r: setattr(r.request, "url", r.request.url.replace(SERVER, "other")),
            lambda r: setattr(r.request, "method", "GET"),
            lambda r: setattr(r.request, "post_data_json", {"variantId": "pool1", "quantity": 1}),
        ):
            response = self.paid_response()
            mutate(response)
            with self.assertRaises(OneCentUIError):
                self.normalize(response)
            response.body.assert_not_called()

    def test_paid_response_body_and_length_are_bounded(self):
        response = self.paid_response()
        response.header_value.return_value = str(onecent_ui.PAID_RESPONSE_LIMIT + 1)
        with self.assertRaises(OneCentUIError):
            self.normalize(response)
        response.body.assert_not_called()
        response = self.paid_response(raw=b"x" * (onecent_ui.PAID_RESPONSE_LIMIT + 1))
        response.header_value.return_value = None
        with self.assertRaises(OneCentUIError):
            self.normalize(response)
        for length in ("-1", "not-a-length", 50):
            response = self.paid_response()
            response.header_value.return_value = length
            with self.assertRaises(OneCentUIError):
                self.normalize(response)
            response.body.assert_not_called()

    def test_only_exact_consumed_request_can_supply_paid_proof(self):
        self.prepare_payment()
        route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        other = self.paid_response()
        exact = self.paid_response(route.request)
        button = locator()
        def click(**_):
            self.service._guard(route)
            self.page.on.call_args.args[1](other)
        button.click.side_effect = click
        self.page.wait_for_event.return_value = exact
        proof = self.submit(button)
        other.body.assert_not_called()
        exact.body.assert_called_once()
        self.assertEqual(proof["idempotency_key"], KEY)
        predicate = self.page.wait_for_event.call_args.kwargs["predicate"]
        self.assertFalse(predicate(other))
        self.assertTrue(predicate(exact))
        button.click.assert_called_once()

    def test_late_exact_response_after_click_error_can_finish_without_another_click(self):
        self.prepare_payment()
        route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        response = self.paid_response(route.request)
        button = locator()
        def click(**_):
            self.service._guard(route)
            raise RuntimeError("private click timeout")
        button.click.side_effect = click
        def late(_event, *, predicate, timeout):
            self.assertEqual(timeout, onecent_ui.PAID_RESPONSE_WAIT_MS)
            self.assertTrue(predicate(response))
            self.page.on.call_args.args[1](response)
            return response
        self.page.wait_for_event.side_effect = late
        self.assertEqual(self.submit(button)["amount_rub"], "12.90")
        response.body.assert_called_once()
        button.click.assert_called_once()
        self.page.wait_for_event.assert_called_once()
        self.page.remove_listener.assert_called_once()
        self.assertIsNotNone(self.service._guard)
        self.assertFalse(self.service._allow_payment)

    def test_missing_response_is_held_without_reclick_or_guard_removal(self):
        self.prepare_payment()
        route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        button = locator()
        button.click.side_effect = lambda **_: self.service._guard(route)
        self.page.wait_for_event.side_effect = TimeoutError("private response timeout")
        with self.assertRaises(OneCentUIError) as caught:
            self.submit(button)
        self.assertNotIn("private", str(caught.exception))
        button.click.assert_called_once()
        self.page.wait_for_event.assert_called_once()
        self.assertIsNotNone(self.service._guard)
        self.assertFalse(self.service._allow_payment)
        duplicate = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        self.service._guard(duplicate)
        duplicate.abort.assert_called_once()

    def test_response_callback_failure_and_distinct_duplicate_responses_are_held(self):
        for failure in ("body-error", "second-response"):
            with self.subTest(failure=failure):
                self.service = OneCentPage(self.page, SERVER, "VPS demo")
                self.prepare_payment()
                route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
                first, second = self.paid_response(route.request), self.paid_response(route.request)
                if failure == "body-error":
                    first.body.side_effect = RuntimeError("private callback failure")
                button = locator()
                def click(**_):
                    self.service._guard(route)
                    observed = self.page.on.call_args.args[1]
                    observed(first)
                    if failure == "second-response": observed(second)
                button.click.side_effect = click
                with self.assertRaises(OneCentUIError) as caught:
                    self.submit(button)
                self.assertNotIn("private", str(caught.exception))
                button.click.assert_called_once()
                self.assertFalse(self.service._allow_payment)

    def test_repeated_delivery_of_same_response_object_is_not_a_second_paid_proof(self):
        self.prepare_payment()
        route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        response, button = self.paid_response(route.request), locator()
        def click(**_):
            self.service._guard(route)
            observed = self.page.on.call_args.args[1]
            observed(response)
            observed(response)
        button.click.side_effect = click
        self.assertEqual(self.submit(button)["basis"], "guarded_paid_post_v1")
        response.body.assert_called_once()

    def test_current_quote_opens_final_confirmation_under_write_guard_without_paying(self):
        panel, initial, final = Mock(), locator(), locator(0)
        self.service._quote_kind = "current"
        variant = PoolVariant("pool1", "Дополнительный адрес", Decimal("30.00"))
        buttons = {"Добавить": initial, "Да, добавить": final, "Заказать IPv4": locator()}
        with (
            patch.object(self.service, "_ipv4_section"),
            patch.object(self.service, "_assert_service"),
            patch.object(self.service, "_quote_panel", return_value=panel),
            patch.object(self.service, "_load_current_variant", return_value=variant),
            patch.object(self.service, "_assert_selected_variant"),
            patch.object(self.service, "_read_quote", return_value=self.quote),
            patch("spn_ip_failover.onecent_ui._heading", return_value=locator(0)),
            patch("spn_ip_failover.onecent_ui._button", side_effect=lambda _, name: buttons[name]),
        ):
            self.assertEqual(self.service.open_quote(), self.quote)
        initial.click.assert_called_once()
        final.click.assert_not_called()
        self.assertFalse(self.service._allow_payment)
        self.assertFalse(self.service._payment_sent)
        self.assertEqual(self.service._quote_variant, variant)

    def test_old_modal_seen_during_current_ui_hydration_is_only_cancelled_then_retried(self):
        panel, initial, final, close = Mock(), locator(), locator(0), locator()
        variant = PoolVariant("pool1", "Дополнительный адрес", Decimal("30.00"))
        kinds = iter(("legacy", "current"))
        def quote_panel():
            self.service._quote_kind = next(kinds)
            return panel
        buttons = {"Добавить": initial, "Да, добавить": final,
                   "Заказать IPv4": locator(), "Закрыть": close}
        with (
            patch.object(self.service, "_ipv4_section"),
            patch.object(self.service, "_assert_service"),
            patch.object(self.service, "_quote_panel", side_effect=quote_panel),
            patch.object(self.service, "_load_current_variant", return_value=variant),
            patch.object(self.service, "_assert_selected_variant"),
            patch.object(self.service, "_read_quote", return_value=self.quote),
            patch("spn_ip_failover.onecent_ui._heading", return_value=locator(0)),
            patch("spn_ip_failover.onecent_ui._button", side_effect=lambda _, name: buttons[name]),
            patch("spn_ip_failover.onecent_ui.time.sleep"),
        ):
            self.assertEqual(self.service.open_quote(), self.quote)
        close.click.assert_called_once()
        initial.click.assert_called_once()
        final.click.assert_not_called()

    def test_radio_missing_its_price_during_hydration_is_waited_for_not_assumed(self):
        with (patch.object(self.service, "_assert_selected_variant", side_effect=[OneCentUIError("loading"), None]) as read,
              patch("spn_ip_failover.onecent_ui.time.sleep") as sleep):
            self.service._wait_selected_variant(Mock())
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once()

    def test_current_final_price_waits_until_preview_and_confirmation_are_ready(self):
        self.service._quote_kind = "current"
        self.service._quote_variant = PoolVariant("pool1", "Дополнительный адрес", Decimal("100.00"))
        panel, final = Mock(), locator()
        final.is_enabled.side_effect = [False, True]
        panel.inner_text.return_value = (
            "Адрес в месяц 100,00 ₽ За остаток периода (28 дн.) 93,33 ₽ "
            "К оплате сейчас 93,33 ₽ Далее 100,00 ₽ в месяц при продлении."
        )
        with (patch.object(self.service, "_assert_selected_variant"),
              patch("spn_ip_failover.onecent_ui._button", side_effect=lambda _, name: final if name == "Да, добавить" else locator(0)),
              patch("spn_ip_failover.onecent_ui.time.sleep") as sleep):
            quote = self.service._read_quote(panel)
        self.assertEqual(quote, Quote(Decimal("93.33"), Decimal("100.00")))
        sleep.assert_called_once()
        panel.inner_text.assert_called_once()

    def test_current_variant_must_be_the_only_checked_radio_and_exact_normal_name_price(self):
        self.service._quote_variant = PoolVariant("pool1", "Дополнительный адрес", Decimal("30.00"))
        panel, normal, chosen = Mock(), locator(), locator()
        normal.get_attribute.return_value = "true"
        chosen.get_attribute.return_value = "false"
        normal.inner_text.return_value = "Дополнительный адрес\n30,00\u00a0₽ в месяц"
        chosen.inner_text.return_value = "IP на выбор\n+1 000,00 ₽ разово"
        panel.get_by_role.return_value.all.return_value = [normal, chosen]
        self.service._assert_selected_variant(panel)
        for text, checked in (("IPWatch 30,00 ₽ в месяц", "false"),
                              ("Дополнительный адрес 31,00 ₽ в месяц", "false"),
                              ("Дополнительный адрес 30,00 ₽ в месяц", "true")):
            normal.inner_text.return_value = text
            chosen.get_attribute.return_value = checked
            with self.subTest(text=text, checked=checked), self.assertRaises(OneCentUIError):
                self.service._assert_selected_variant(panel)

    def test_current_order_unknown_result_retains_guard_and_cannot_be_reclicked(self):
        self.service._install_quote_guard()
        self.service._quote_kind = "current"
        variant = PoolVariant("pool1", "Дополнительный адрес", Decimal("30.00"))
        self.service._quote_variant = variant
        self.service._opened_quote = self.quote
        button = locator()
        def paid_then_timeout(**_):
            route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
            self.service._guard(route)
            route.continue_.assert_called_once()
            raise RuntimeError("lost response")
        button.click.side_effect = paid_then_timeout
        with (
            patch.object(self.service, "_quote_panel"),
            patch.object(self.service, "_read_quote", return_value=self.quote),
            patch.object(self.service, "_load_current_variant", return_value=variant),
            patch.object(self.service, "_assert_service"),
            patch("spn_ip_failover.onecent_ui._button", return_value=button),
            self.assertRaises(OneCentUIError),
        ):
            self.service.submit_order(self.quote, request_id=REQUEST, idempotency_key=KEY)
        self.assertIsNotNone(self.service._guard)
        self.assertFalse(self.service._allow_payment)
        with self.assertRaises(OneCentUIError):
            self.service.submit_order(self.quote, request_id=REQUEST, idempotency_key=KEY)
        duplicate = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        self.service._guard(duplicate)
        duplicate.abort.assert_called_once()
        button.click.assert_called_once()

    def test_current_order_success_authorizes_one_click_and_keeps_late_retry_blocked(self):
        self.service._install_quote_guard()
        self.service._quote_kind = "current"
        variant = PoolVariant("pool1", "Дополнительный адрес", Decimal("30.00"))
        self.service._quote_variant = variant
        self.service._opened_quote = self.quote
        button = locator()
        route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        response = self.paid_response(route.request)
        def submit(**_):
            self.service._guard(route)
            self.page.on.call_args.args[1](response)
        button.click.side_effect = submit
        with (
            patch.object(self.service, "_quote_panel"),
            patch.object(self.service, "_read_quote", return_value=self.quote) as read,
            patch.object(self.service, "_load_current_variant", return_value=variant),
            patch.object(self.service, "_assert_service"),
            patch("spn_ip_failover.onecent_ui._button", return_value=button),
            patch("spn_ip_failover.onecent_ui._heading"),
        ):
            proof = self.service.submit_order(self.quote, request_id=REQUEST, idempotency_key=KEY)
        self.assertEqual(read.call_count, 2)
        route.continue_.assert_called_once()
        button.click.assert_called_once()
        self.assertIsNone(self.service._opened_quote)
        self.assertIsNotNone(self.service._guard)
        self.assertFalse(self.service._allow_payment)
        self.assertEqual(proof["address"], None)
        self.assertEqual(proof["amount_rub"], "12.90")
        self.assertNotIn("ip", proof)

    def test_close_current_quote_uses_cancel_and_removes_guard(self):
        self.service._install_quote_guard()
        guard = self.service._guard
        self.service._quote_kind = "current"
        self.service._opened_quote = self.quote
        with (patch.object(self.service, "_quote_panel") as panel,
              patch("spn_ip_failover.onecent_ui._button") as button,
              patch("spn_ip_failover.onecent_ui._heading")):
            self.service.close_quote()
        button.assert_called_once_with(panel.return_value, "Отмена")
        self.page.unroute.assert_called_once_with("**/*", guard)
        self.assertIsNone(self.service._guard)
        self.assertIsNone(self.service._opened_quote)

    def test_service_url_rejects_unexpected_host_and_id(self):
        self.service._assert_url()
        valid = self.page.url
        for url in (valid.replace("bill.1cent.host", "bill.1cent.host.example"), valid + "0"):
            self.page.url = url
            with self.assertRaises(OneCentUIError):
                self.service._assert_url()

    def test_open_recovers_login_once_then_reopens_exact_service(self):
        service_url = self.page.url
        login = Mock()
        self.service = OneCentPage(self.page, SERVER, "VPS demo", login=login)
        self.service._opened_quote = self.quote
        destinations = iter(["https://bill.1cent.host/login", service_url])

        def navigate(*_, **__):
            self.page.url = next(destinations)
            return Mock(ok=True)

        self.page.goto.side_effect = navigate
        with patch("spn_ip_failover.onecent_ui._heading", return_value=locator()):
            self.service.open()
        login.assert_called_once_with()
        self.assertEqual(self.page.goto.call_count, 2)
        self.assertTrue(all(call.args == (service_url,) for call in self.page.goto.call_args_list))
        self.assertIsNone(self.service._opened_quote)

    def test_login_loop_and_unexpected_login_origin_never_repeat_login(self):
        for destination in ("https://bill.1cent.host/login", "https://bill.1cent.host.example/login"):
            login = Mock()
            service = OneCentPage(self.page, SERVER, "VPS demo", login=login)
            self.page.goto.reset_mock(side_effect=True)

            def navigate(*_, **__):
                self.page.url = destination
                return Mock(ok=True)

            self.page.goto.side_effect = navigate
            with self.subTest(destination=destination), self.assertRaises(OneCentUIError):
                service.open()
            expected = 1 if destination == "https://bill.1cent.host/login" else 0
            self.assertEqual(login.call_count, expected)
            self.assertEqual(self.page.goto.call_count, expected + 1)

    def test_quote_is_reread_and_a_changed_price_never_clicks(self):
        self.service._opened_quote = self.quote
        button = locator()
        changed = Quote(Decimal("13.00"), Decimal("30.00"))
        with (
            patch.object(self.service, "_quote_panel"),
            patch.object(self.service, "_read_quote", return_value=changed),
            patch("spn_ip_failover.onecent_ui._button", return_value=button),
            self.assertRaisesRegex(OneCentUIError, "изменилась"),
        ):
            self.service.submit_order(self.quote, request_id=REQUEST, idempotency_key=KEY)
        button.click.assert_not_called()

    def test_inventory_waits_for_complete_stable_rows_during_page_hydration(self):
        primary = (Mock(), Address("144.31.171.189", True))
        additional = (Mock(), Address("2.26.148.130", False))
        expected = [additional, primary]
        with (
            patch.object(self.service, "_assert_service"),
            patch.object(self.service, "_read_rows", side_effect=[
                OneCentUIError("partial row"), [primary], expected, expected,
            ]) as read,
            patch("spn_ip_failover.onecent_ui.time.sleep"),
        ):
            self.assertEqual(self.service._rows(Mock()), expected)
        self.assertEqual(read.call_count, 4)

    def test_incomplete_inventory_stops_without_any_mutation(self):
        with (
            patch.object(self.service, "_assert_service"),
            patch.object(self.service, "_read_rows", side_effect=OneCentUIError("partial row")),
            patch("spn_ip_failover.onecent_ui.time.monotonic", side_effect=[0, 9]),
            self.assertRaisesRegex(OneCentUIError, "не загрузилась"),
        ):
            self.service._rows(Mock())
        self.page.click.assert_not_called()

    def test_quote_waits_for_real_price_and_requires_quantity_one(self):
        panel, number = Mock(), locator()
        panel.inner_text.side_effect = [
            "Цена за адрес … К оплате сейчас …",
            "Цена за адрес 30,00 ₽ / мес. Срок до конца аренды сервера, 13 дн. "
            "К оплате сейчас 12,90 ₽ Адрес оплачивается сразу за весь оставшийся срок аренды сервера. "
            "Дальше продлевается вместе с сервером: +30,00 ₽ в месяц.",
        ]
        number.inner_text.return_value = "1"
        with (
            patch("spn_ip_failover.onecent_ui._ancestor"),
            patch("spn_ip_failover.onecent_ui._button", return_value=locator()),
            patch("spn_ip_failover.onecent_ui._text", return_value=number),
            patch("spn_ip_failover.onecent_ui.time.sleep") as wait,
        ):
            self.assertEqual(self.service._read_quote(panel), self.quote)
            wait.assert_called_once()
            number.inner_text.return_value = "2"
            with self.assertRaisesRegex(OneCentUIError, "ровно один"):
                self.service._read_quote(panel)
        self.assertEqual(panel.inner_text.call_count, 2)

    def test_order_cannot_be_submitted_twice_from_one_quote(self):
        self.prepare_payment()
        button = locator()
        route = self.request_route("POST", f"/api/services/{SERVER}/ipv4/add", {"variantId": "pool1"})
        response = self.paid_response(route.request)
        def submit(**_):
            self.service._guard(route)
            self.page.on.call_args.args[1](response)
        button.click.side_effect = submit
        with (
            patch.object(self.service, "_quote_panel"),
            patch.object(self.service, "_read_quote", return_value=self.quote),
            patch.object(self.service, "_assert_service"),
            patch.object(self.service, "_load_current_variant", return_value=self.service._quote_variant),
            patch("spn_ip_failover.onecent_ui._button", return_value=button),
            patch("spn_ip_failover.onecent_ui._heading"),
        ):
            self.service.submit_order(self.quote, request_id=REQUEST, idempotency_key=KEY)
            with self.assertRaises(OneCentUIError):
                self.service.submit_order(self.quote, request_id=REQUEST, idempotency_key=KEY)
        button.click.assert_called_once()

    def test_primary_address_does_not_resolve_delete_button(self):
        with (
            patch.object(self.service, "_ipv4_section"),
            patch.object(self.service, "_rows", return_value=[(Mock(), Address("8.8.8.8", True))]),
            patch("spn_ip_failover.onecent_ui._button") as button,
            self.assertRaisesRegex(OneCentUIError, "дополнительный"),
        ):
            self.service.remove_additional("8.8.8.8")
        button.assert_not_called()

    def test_primary_removal_requires_exact_old_and_replacement_rows(self):
        main = Address("8.8.8.8", True)
        replacement = Address("9.9.9.9", False)
        extra = Address("1.1.1.1", False)
        with (
            patch.object(self.service, "_ipv4_section") as section,
            patch.object(self.service, "_rows") as rows,
            patch.object(self.service, "_remove_address") as delete,
        ):
            for entries in ((main,), (main, extra), (main, replacement, extra),
                            (Address(main.ip, False), Address(replacement.ip, True))):
                rows.return_value = [(Mock(), entry) for entry in entries]
                with self.subTest(entries=entries), self.assertRaisesRegex(OneCentUIError, "ровно старый"):
                    self.service.remove_primary(main.ip, replacement.ip)
            delete.assert_not_called()
            rows.return_value = [(Mock(), main), (Mock(), replacement)]
            self.service.remove_primary(main.ip, replacement.ip)
        delete.assert_called_once_with(section.return_value, main.ip, "Главный", before_submit=None,
                                       idempotency_key=None, replacement=replacement.ip, expected_inventory=None)

    def test_unknown_native_confirmation_is_dismissed_and_not_accepted(self):
        button = locator()
        confirmation = self.release_prompt()
        confirmation.message = "Unknown confirmation for another address"
        def click(**_):
            self.event_handler("dialog")(confirmation)
        button.click.side_effect = click
        with self.assertRaisesRegex(OneCentUIError, "не подтверждено"):
            self.remove(button)
        button.click.assert_called_once()
        confirmation.dismiss.assert_called_once()
        confirmation.accept.assert_not_called()

    def test_release_is_bound_to_exact_index_confirmed_target_and_saved_key(self):
        button, route, prompt = locator(), self.release_route(), self.release_prompt()
        response = self.release_response(route.request)
        saved = []
        route.request.header_value.return_value = "different-browser-key"
        route.request.headers = {"content-type": "application/json", "idempotency-key": "different-browser-key"}
        def confirmed():
            self.assertEqual(saved, [KEY])
            self.service._release_guard(route)
            self.event_handler("response")(response)
        prompt.accept.side_effect = confirmed
        button.click.side_effect = lambda **_: self.event_handler("dialog")(prompt)
        proof = self.remove(button, before_submit=lambda: saved.append(KEY))
        self.assertEqual(proof, {"format": 1, "basis": "guarded_release_v1", "service_id": SERVER,
                                "idempotency_key": KEY, "method": "DELETE",
                                "path": f"/api/services/{SERVER}/ipv4/2", "http_status": 200,
                                "released": "9.9.9.9", "refunded_rub": "12.34",
                                "response_sha256": hashlib.sha256(response.body.return_value).hexdigest()})
        route.continue_.assert_called_once_with(headers={"content-type": "application/json", "Idempotency-Key": KEY})
        prompt.accept.assert_called_once()
        prompt.dismiss.assert_not_called()
        button.click.assert_called_once()
        self.assertIsNotNone(self.service._release_guard)
        duplicate = self.release_route()
        self.service._release_guard(duplicate)
        duplicate.abort.assert_called_once()

    def test_release_guard_rejects_protected_indices_query_body_and_nondelete(self):
        button, prompt, exact = locator(), self.release_prompt(), self.release_route()
        response = self.release_response(exact.request)
        rejected = [self.release_route(path=f"/api/services/{SERVER}/ipv4/0"),
                    self.release_route(path=f"/api/services/{SERVER}/ipv4/1"),
                    self.release_route(path=f"/api/services/{SERVER}/ipv4/2?other=1"),
                    self.release_route(body="null"), self.release_route(body='{"id":2}')]
        nondelete = self.release_route()
        nondelete.request.method = "POST"
        rejected.append(nondelete)
        def accepted():
            for route in rejected:
                self.service._release_guard(route)
            self.service._release_guard(exact)
            self.event_handler("response")(response)
        prompt.accept.side_effect = accepted
        button.click.side_effect = lambda **_: self.event_handler("dialog")(prompt)
        self.remove(button)
        for route in rejected:
            route.abort.assert_called_once()
            route.continue_.assert_not_called()
        exact.continue_.assert_called_once()

    def test_release_cannot_dispatch_without_the_exact_native_confirmation(self):
        button, route = locator(), self.release_route()
        button.click.side_effect = lambda **_: self.service._release_guard(route)
        with self.assertRaises(OneCentUIError):
            self.remove(button)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()
        button.click.assert_called_once()
        late = self.release_route()
        self.service._release_guard(late)
        late.abort.assert_called_once()

    def test_release_unknown_refund_or_target_native_prompt_is_dismissed(self):
        for change in (("9.9.9.9", "1.1.1.1"), ("12.34", "12.35")):
            with self.subTest(change=change):
                self.service = OneCentPage(self.page, SERVER, "VPS demo")
                button, prompt = locator(), self.release_prompt()
                prompt.message = prompt.message.replace(*change)
                button.click.side_effect = lambda **_: self.event_handler("dialog")(prompt)
                with self.assertRaises(OneCentUIError):
                    self.remove(button)
                prompt.dismiss.assert_called_once()
                prompt.accept.assert_not_called()

    def test_release_preflight_rejects_changed_busy_noninteger_or_manual_role_drift(self):
        base = self.release_snapshot()
        variants = []
        for field, value in (("id", "2"), ("id", True), ("id", 1), ("removable", False)):
            changed = deepcopy(base)
            changed["addresses"][2][field] = value
            variants.append([changed, changed])
        drift = deepcopy(base)
        drift["addresses"][2]["id"] = 3
        variants.append([base, drift])
        busy = deepcopy(base)
        busy["operation"] = {"id": "unresolved"}
        variants.append([busy, busy])
        order = deepcopy(base)
        order["orderFlow"]["order"] = {"id": "pending"}
        variants.append([order, order])
        for snapshots in variants:
            with self.subTest(snapshots=snapshots):
                self.service = OneCentPage(self.page, SERVER, "VPS demo")
                button, marker = locator(), Mock()
                with self.assertRaises((OneCentUIError, ValueError)):
                    self.remove(button, snapshots=snapshots, before_submit=marker)
                button.click.assert_not_called()
                marker.assert_not_called()

    def test_release_requires_the_exact_boolean_protected_baseline(self):
        for expected in ({"8.8.8.8": True, "9.9.9.9": False},
                         {"8.8.8.8": 1, "1.1.1.1": False, "9.9.9.9": False},
                         {"8.8.8.8": True, "1.1.1.1": True, "9.9.9.9": False}):
            button, marker = locator(), Mock()
            with self.subTest(expected=expected), self.assertRaises(OneCentUIError):
                self.remove(button, before_submit=marker, expected_inventory=expected)
            marker.assert_not_called()
            button.click.assert_not_called()

    def test_release_missing_key_and_missing_durable_callback_never_click(self):
        for kwargs in ({"before_submit": Mock(), "idempotency_key": None},
                       {"before_submit": None, "idempotency_key": KEY}):
            with patch("spn_ip_failover.onecent_ui._button") as button, self.assertRaises(OneCentUIError):
                self.service._remove_address(Mock(), "9.9.9.9", "Дополнительный",
                                             expected_inventory={"8.8.8.8": True, "9.9.9.9": False}, **kwargs)
            button.assert_not_called()

    def test_release_durable_marker_failure_never_clicks_or_forwards(self):
        button, marker = locator(), Mock(side_effect=OSError("private journal failure"))
        with self.assertRaises(OSError):
            self.remove(button, before_submit=marker)
        button.click.assert_not_called()
        route = self.release_route()
        self.service._release_guard(route)
        route.abort.assert_called_once()

    def test_late_release_response_after_click_timeout_is_captured_once_without_retry(self):
        button, route, prompt = locator(), self.release_route(), self.release_prompt()
        response = self.release_response(route.request)
        prompt.accept.side_effect = lambda: self.service._release_guard(route)
        def click(**_):
            self.event_handler("dialog")(prompt)
            raise RuntimeError("private click timeout")
        button.click.side_effect = click
        self.page.wait_for_event.return_value = response
        self.assertEqual(self.remove(button)["released"], "9.9.9.9")
        button.click.assert_called_once()
        self.page.wait_for_event.assert_called_once()
        response.body.assert_called_once()
        duplicate = self.release_route()
        self.service._release_guard(duplicate)
        duplicate.abort.assert_called_once()

    def test_release_response_callback_failure_is_held_and_same_ui_cannot_reclick(self):
        button, route, prompt = locator(), self.release_route(), self.release_prompt()
        response = self.release_response(route.request)
        response.body.side_effect = RuntimeError("private callback failure")
        def accepted():
            self.service._release_guard(route)
            self.event_handler("response")(response)
        prompt.accept.side_effect = accepted
        button.click.side_effect = lambda **_: self.event_handler("dialog")(prompt)
        with self.assertRaises(OneCentUIError) as caught:
            self.remove(button)
        self.assertNotIn("private", str(caught.exception))
        with self.assertRaises(OneCentUIError):
            self.remove(button)
        button.click.assert_called_once()
        duplicate = self.release_route()
        self.service._release_guard(duplicate)
        duplicate.abort.assert_called_once()

    def test_release_response_requires_exact_target_refund_success_and_shape(self):
        for raw, status in (
            (b'{"released":"1.1.1.1","refunded":"12.34"}', 200),
            (b'{"released":"9.9.9.9","refunded":"12.35"}', 200),
            (b'{"released":"9.9.9.9","refunded":12.34}', 200),
            (b'{"released":"9.9.9.9","refunded":"12.340"}', 200),
            (b'{"released":"9.9.9.9","refunded":"12.34","other":true}', 200),
            (b'{"released":"9.9.9.9","released":"9.9.9.9","refunded":"12.34"}', 200),
            (b'{"released":"9.9.9.9","refunded":"12.34"}', 400),
        ):
            route = self.release_route()
            response = self.release_response(route.request, raw=raw, status=status)
            with self.subTest(raw=raw, status=status), self.assertRaises(OneCentUIError):
                onecent_ui.normalize_release_response(response, service_id=SERVER, target="9.9.9.9",
                                                       row_id=2, idempotency_key=KEY, expected_refund=Decimal("12.34"))
