from decimal import Decimal
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import subscription_renewal as renewal
import webhooks
from services import yookassa
from services.discounts import current_price


TOKEN_URL = "https://sub.wayspn.online/TestSecretAbCd12"
REMOTE_UUID = "ade4cd4d-34e7-4491-a22e-c5e8ac2f183e"
INVOICE_ID = "32277abd-000f-5000-b000-1b46db3fc1db"
REQUEST_ID = "1eae8169-308d-4f77-a25d-916c2d4dcb6e"


def subscription(**changes):
    result = {
        "id": 77, "tg_id": 123, "plan_kind": "regular", "type_index": 2,
        "generation": "v2", "is_visible": True, "is_renewable": True,
        "legacy_readonly": False, "remnawave_uuid": REMOTE_UUID,
        "subscription_until": datetime.now(timezone.utc) + timedelta(days=20),
    }
    result.update(changes)
    return result


def payment(**changes):
    result = {
        "tg_id": 123, "subscription_id": 77, "tariff_code": "regular_1m",
        "amount": Decimal("200.00"), "invoice_id": INVOICE_ID,
        "payment_kind": "subscription", "payment_target": "renew",
        "provider": "yookassa", "status": "pending",
    }
    result.update(changes)
    return result


class RenewalAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        renewal._attempts.clear()
        self.subscription = subscription()
        self.payment = payment()
        self.resolve = self.enterContext(patch.object(renewal, "remnawave_resolve_user_uuid_by_short_uuid",
                                                     AsyncMock(return_value=REMOTE_UUID)))
        self.lookup = self.enterContext(patch.object(renewal.db, "get_subscription_by_uuid",
                                                    AsyncMock(side_effect=lambda _: self.subscription)))
        self.reload = self.enterContext(patch.object(renewal.db, "get_subscription_by_id",
                                                    AsyncMock(side_effect=lambda *args: self.subscription)))
        self.enterContext(patch.object(renewal, "refresh_subscription_expiry",
                                      AsyncMock(side_effect=lambda sub: sub["subscription_until"])))
        self.discounts = self.enterContext(patch.object(renewal.db, "get_active_discounts", AsyncMock(return_value=[])))
        self.special_discounts = self.enterContext(patch.object(renewal.db, "get_eligible_special_discounts", AsyncMock(return_value=[])))
        self.pending_special = self.enterContext(patch.object(renewal.db, "get_pending_special_discount_payment",
                                                             AsyncMock(return_value=None)))
        self.pending = self.enterContext(patch.object(renewal.db, "get_pending_renewal_for_subscription",
                                                     AsyncMock(return_value=None)))
        self.acquire = self.enterContext(patch.object(renewal.db, "acquire_user_lock", AsyncMock(return_value=True)))
        self.release = self.enterContext(patch.object(renewal.db, "release_user_lock", AsyncMock()))
        self.record = self.enterContext(patch.object(renewal.db, "create_renewal_checkout_payment",
                                                    AsyncMock(side_effect=lambda *args, **kwargs: self.payment)))
        self.get_payment = self.enterContext(patch.object(renewal.db, "get_payment_by_invoice",
                                                         AsyncMock(side_effect=lambda _: self.payment)))
        self.provider = self.enterContext(patch.object(renewal, "create_yookassa_payment", AsyncMock(return_value={
            "id": INVOICE_ID, "amount": {"value": "200.00"},
            "confirmation": {"confirmation_url": "https://yoomoney.ru/checkout/test"},
        })))
        self.reconcile = self.enterContext(patch.object(renewal, "reconcile_payment", AsyncMock()))
        self.enterContext(patch.object(renewal, "PUBLIC_SITE_URL", "https://wayspn.ru"))
        self.enterContext(patch("services.remnawave.SUBSCRIPTION_PUBLIC_BASE_URL", "https://sub.wayspn.online"))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=webhooks.app), base_url="https://wayspn.ru",
            headers={"Authorization": f"Bearer {TOKEN_URL}"},
        ))
        self.body = {"tariff_code": "regular_1m", "request_id": REQUEST_ID, "expected_price": 200}

    async def test_page_is_private_and_has_self_only_csp(self):
        response = await self.client.get("/renew")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Продлить подписку", response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertNotIn(TOKEN_URL, response.text)

    async def test_missing_bearer_is_not_authorized_by_a_website_cookie(self):
        response = await self.client.get("/site/api/renewal/details", headers={"Authorization": "", "Cookie": "wayspn_web_session=anything"})
        self.assertEqual(response.status_code, 401)
        self.resolve.assert_not_awaited()
        self.assertIn("no-store", response.headers["cache-control"])

    async def test_credential_does_not_open_the_account(self):
        response = await self.client.get("/site/api/me")
        self.assertEqual(response.status_code, 401)

    async def test_bad_subscription_urls_never_cause_remote_requests(self):
        bad = ["http://sub.wayspn.online/TestSecretAbCd12", "https://evil.example/TestSecretAbCd12",
               "https://sub.wayspn.online.evil.example/TestSecretAbCd12",
               "https://sub.wayspn.online:444/TestSecretAbCd12", "https://user@sub.wayspn.online/TestSecretAbCd12",
               "https://sub.wayspn.online/short", "https://sub.wayspn.online/TestSecretAbCd12?x=1",
               "https://sub.wayspn.online/TestSecretAbCd12#foo", "https://sub.wayspn.online/a/b/c",
               "https://sub.wayspn.online/%2e%2e%2fabc"]
        for token in bad:
            with self.subTest(token=token):
                response = await self.client.get("/site/api/renewal/details", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(response.status_code, 401)
        self.resolve.assert_not_awaited()

    async def test_revoked_url_fails_closed(self):
        self.resolve.return_value = None
        response = await self.client.get("/site/api/renewal/details")
        self.assertEqual(response.status_code, 401)
        self.lookup.assert_not_awaited()

    async def test_panel_outage_does_not_echo_credential_or_allow_checkout(self):
        self.resolve.side_effect = RuntimeError(TOKEN_URL)
        response = await self.client.post("/site/api/renewal/payment", json=self.body)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(TOKEN_URL, response.text)
        self.provider.assert_not_awaited()

    async def test_not_renewable_subscriptions_are_rejected(self):
        for changes in ({"is_visible": False}, {"is_renewable": False}, {"generation": "v1"},
                        {"legacy_readonly": True}, {"plan_kind": "unknown"}, {"remnawave_uuid": None}):
            self.subscription = subscription(**changes)
            with self.subTest(changes=changes):
                response = await self.client.get("/site/api/renewal/details")
                self.assertEqual(response.status_code, 401)

    async def test_expired_subscription_can_still_be_renewed(self):
        self.subscription["subscription_until"] = datetime.now(timezone.utc) - timedelta(days=2)
        response = await self.client.get("/site/api/renewal/details")
        self.assertEqual(response.status_code, 200)

    async def test_cross_origin_browser_requests_are_rejected(self):
        response = await self.client.post("/site/api/renewal/payment", json=self.body, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.provider.assert_not_awaited()

    async def test_same_origin_checkout_is_allowed(self):
        response = await self.client.post("/site/api/renewal/payment", json=self.body, headers={"Origin": "https://wayspn.ru"})
        self.assertEqual(response.status_code, 200)

    async def test_details_only_expose_this_subscriptions_safe_summary(self):
        self.pending.return_value = {"invoice_id": INVOICE_ID}
        response = await self.client.get("/site/api/renewal/details?subscription_id=999&tg_id=999")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["subscription"]["name"], "Обычная #2")
        self.assertEqual({t["code"] for t in result["tariffs"]}, {"regular_1m", "regular_3m"})
        self.assertEqual(result["pending_invoice_id"], INVOICE_ID)
        self.pending.assert_awaited_once_with(77, 123)
        for sensitive in (TOKEN_URL, REMOTE_UUID, "tg_id", "subscription_url", "vlessUuid", "username"):
            self.assertNotIn(sensitive, response.text)

    async def test_bypass_subscriptions_have_only_bypass_tariffs(self):
        self.subscription["plan_kind"] = "bypass"
        result = (await self.client.get("/site/api/renewal/details")).json()
        self.assertEqual({t["code"] for t in result["tariffs"]}, {"bypass_1m", "bypass_3m"})

    async def test_catalog_uses_the_same_discounts_as_bot(self):
        self.discounts.return_value = [{"id": 1, "name": "Test", "discount_type": "percent", "value": 20, "target_type": "all"}]
        result = (await self.client.get("/site/api/renewal/details")).json()
        self.assertEqual(result["tariffs"][0]["price"], 160)
        self.assertEqual(result["tariffs"][0]["original_price"], 200)

    async def test_special_price_matches_tariff_and_renewal_and_is_saved_with_invoice(self):
        self.special_discounts.return_value = [{
            "id": 45, "name": "Возвращение", "percent": 75,
            "tariff_codes": ["regular_1m"], "payment_target": "renew",
        }]
        self.provider.return_value["amount"] = {"value": "50.00"}
        self.payment["amount"] = Decimal("50.00")
        details = (await self.client.get("/site/api/renewal/details")).json()["tariffs"]
        self.assertEqual(details[0]["price"], 50)
        self.assertNotIn("special_discount_id", details[1])
        self.assertEqual((await current_price(200, product_type="subscription", code="regular_1m",
                                              plan_kind="regular", tg_id=123))["price"], 200)
        response = await self.client.post("/site/api/renewal/payment", json={**self.body, "expected_price": 50})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.provider.await_args.args[1], 50)
        self.assertEqual(self.record.await_args.kwargs, {"special_discount_id": 45})

    async def test_pending_special_invoice_is_reused_only_for_matching_checkout(self):
        self.special_discounts.return_value = [{
            "id": 45, "name": "Возвращение", "percent": 75,
            "tariff_codes": [], "payment_target": "renew",
        }]
        pending = payment(special_discount_id=45, amount=Decimal("50.00"))
        self.pending_special.return_value = pending
        remote = {**self.provider.return_value, "status": "pending"}
        with patch.object(yookassa, "get_payment_status", AsyncMock(return_value=remote)):
            response = await self.client.post("/site/api/renewal/payment", json={**self.body, "expected_price": 50})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["invoice_id"], INVOICE_ID)
            pending["subscription_id"] = 999
            response = await self.client.post("/site/api/renewal/payment", json={**self.body, "expected_price": 50})
            self.assertEqual(response.status_code, 409)
        self.provider.assert_not_awaited()
        self.record.assert_not_awaited()

    async def test_all_renewal_prices_match_bot_with_global_and_targeted_discounts(self):
        cases = [[],
                 [{"id": 1, "name": "Общая", "discount_type": "percent", "value": 20, "target_type": "all"}],
                 [{"id": 2, "name": "Обход", "discount_type": "fixed", "value": 45, "target_type": "bypass"},
                  {"id": 3, "name": "90 дней", "discount_type": "percent", "value": 15,
                   "target_type": "tariff", "target_code": "regular_3m"}]]
        for discounts in cases:
            self.discounts.return_value = discounts
            for kind in ("regular", "bypass"):
                self.subscription["plan_kind"] = kind
                catalog = (await self.client.get("/site/api/renewal/details")).json()["tariffs"]
                for tariff in catalog:
                    with self.subTest(discounts=discounts, tariff=tariff["code"]):
                        expected = await current_price(
                            renewal.TARIFFS[tariff["code"]]["price"], product_type="subscription",
                            code=tariff["code"], plan_kind=kind,
                        )
                        for field in ("price", "original_price", "discount"):
                            self.assertEqual(tariff[field], expected[field])

    async def test_customer_cannot_supply_a_different_subscription_or_price(self):
        for field, value in (("subscription_id", 999), ("tg_id", 999), ("amount", 1), ("payment_target", "new")):
            with self.subTest(field=field):
                response = await self.client.post("/site/api/renewal/payment", json={**self.body, field: value})
                self.assertEqual(response.status_code, 422)
        self.provider.assert_not_awaited()

    async def test_wrong_tariff_kind_is_rejected(self):
        response = await self.client.post("/site/api/renewal/payment", json={**self.body, "tariff_code": "bypass_1m"})
        self.assertEqual(response.status_code, 400)
        self.provider.assert_not_awaited()

    async def test_price_change_or_client_discount_does_not_create_an_invoice(self):
        response = await self.client.post("/site/api/renewal/payment", json={**self.body, "expected_price": 1})
        self.assertEqual(response.status_code, 409)
        self.provider.assert_not_awaited()
        self.release.assert_awaited_once_with(123)

    async def test_subscription_rechecked_under_user_lock(self):
        self.reload.side_effect = None
        self.reload.return_value = subscription(is_visible=False)
        response = await self.client.post("/site/api/renewal/payment", json=self.body)
        self.assertEqual(response.status_code, 409)
        self.provider.assert_not_awaited()
        self.release.assert_awaited_once_with(123)

    async def test_busy_subscription_has_no_checkout_or_wrong_unlock(self):
        self.acquire.return_value = False
        response = await self.client.post("/site/api/renewal/payment", json=self.body)
        self.assertEqual(response.status_code, 409)
        self.provider.assert_not_awaited()
        self.release.assert_not_awaited()

    async def test_checkout_has_no_secret_in_return_url_and_uses_server_owner(self):
        response = await self.client.post("/site/api/renewal/payment", json=self.body)
        self.assertEqual(response.status_code, 200)
        args, kwargs = self.provider.await_args
        self.assertEqual(args, (None, 200.0, "regular_1m", 123))
        self.assertEqual(kwargs["return_url"], "https://wayspn.ru/renew?payment=return")
        self.assertNotIn(TOKEN_URL, kwargs["return_url"])
        self.assertEqual(self.record.await_args.args[:1], (123,))
        self.release.assert_awaited_once_with(123)

    async def test_repeated_checkout_has_the_same_provider_key(self):
        await self.client.post("/site/api/renewal/payment", json=self.body)
        self.payment["status"] = "paid"
        response = await self.client.post("/site/api/renewal/payment", json=self.body)
        first, second = self.provider.await_args_list
        self.assertEqual(first.kwargs["idempotence_key"], second.kwargs["idempotence_key"])
        self.assertEqual(response.json()["status"], "paid")
        self.reconcile.assert_not_awaited()

    async def test_distinct_purchases_have_distinct_provider_keys(self):
        await self.client.post("/site/api/renewal/payment", json=self.body)
        await self.client.post("/site/api/renewal/payment", json={**self.body, "request_id": str(uuid.uuid4())})
        first, second = self.provider.await_args_list
        self.assertNotEqual(first.kwargs["idempotence_key"], second.kwargs["idempotence_key"])

    async def test_provider_failure_releases_lock_and_does_not_record_payment(self):
        for remote in (None, {"id": INVOICE_ID}, {"id": INVOICE_ID, "amount": {"value": "1"}, "confirmation": {"confirmation_url": "https://yoomoney.ru/pay"}}):
            self.provider.return_value = remote
            with self.subTest(remote=remote):
                response = await self.client.post("/site/api/renewal/payment", json=self.body)
                self.assertEqual(response.status_code, 502)
        self.record.assert_not_awaited()
        self.assertEqual(self.release.await_count, 3)

    async def test_invoice_record_is_checked_for_collision(self):
        self.record.side_effect = None
        self.record.return_value = payment(subscription_id=88)
        response = await self.client.post("/site/api/renewal/payment", json=self.body)
        self.assertEqual(response.status_code, 409)

    async def test_status_does_not_reveal_any_other_subscription_invoice(self):
        for changes in ({"tg_id": 999}, {"subscription_id": 88}, {"payment_target": "new"},
                        {"payment_kind": "device_addon"}, {"provider": "cryptobot"}, {"tariff_code": "bypass_1m"}):
            self.payment = payment(**changes)
            with self.subTest(changes=changes):
                response = await self.client.get(f"/site/api/renewal/payments/{INVOICE_ID}")
                self.assertEqual(response.status_code, 404)
        self.reconcile.assert_not_awaited()

    async def test_pending_payment_uses_shared_reconciliation_and_bot(self):
        async def paid(*args, **kwargs):
            self.payment["status"] = "paid"
        self.reconcile.side_effect = paid
        sentinel = object()
        original_bot = getattr(webhooks.app.state, "bot", None)
        webhooks.app.state.bot = sentinel
        try:
            response = await self.client.get(f"/site/api/renewal/payments/{INVOICE_ID}")
            self.assertEqual(response.json()["status"], "paid")
            self.reconcile.assert_awaited_once_with(sentinel, INVOICE_ID, expected_tg_id=123)
            response = await self.client.get(f"/site/api/renewal/payments/{INVOICE_ID}")
            self.assertEqual(response.json()["status"], "paid")
            self.assertEqual(self.reconcile.await_count, 1)
        finally:
            webhooks.app.state.bot = original_bot

    async def test_rate_limit_stops_checkout_before_provider_calls(self):
        for _ in range(10):
            self.assertEqual((await self.client.post("/site/api/renewal/payment", json=self.body)).status_code, 200)
        response = await self.client.post("/site/api/renewal/payment", json=self.body)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.provider.await_count, 10)


class RenewalStorageAndHeadersTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_retry_cannot_reset_paid_invoice(self):
        with (
            patch.object(renewal.db, "get_payment_tracking_code", AsyncMock(return_value=None)),
            patch.object(renewal.db, "db_execute", AsyncMock()) as execute,
            patch.object(renewal.db, "get_payment_by_invoice", AsyncMock(return_value=payment(status="paid"))),
        ):
            result = await renewal.db.create_renewal_checkout_payment(123, subscription(), "regular_1m", 200, INVOICE_ID)
        self.assertEqual(result["status"], "paid")
        sql, params = execute.await_args.args
        self.assertIn("ON CONFLICT (invoice_id) DO NOTHING", sql)
        self.assertNotIn("UPDATE", sql)
        self.assertEqual(params, (123, "regular_1m", 200, INVOICE_ID, 77, 2, None, None))

    async def test_pending_lookup_is_scoped_to_subscription_and_owner(self):
        with patch.object(renewal.db, "db_execute", AsyncMock(return_value=None)) as execute:
            await renewal.db.get_pending_renewal_for_subscription(77, 123)
        sql, params = execute.await_args.args
        self.assertIn("subscription_id = $1 AND tg_id = $2", sql)
        self.assertIn("payment_target = 'renew'", sql)
        self.assertEqual(params, (77, 123))

    def _header_generator(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "prepare_happ_renewal.py"
        spec = importlib.util.spec_from_file_location("happ_renewal_headers", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_free_metadata_uses_individual_subscription_template_without_paid_features(self):
        module = self._header_generator()
        headers = module.build_headers("https://wayspn.ru")
        link = "https://wayspn.ru/renew#{{SUBSCRIPTION_URL}}"
        self.assertEqual(headers["profile-web-page-url"], link)
        self.assertNotIn("announce-url", headers)
        self.assertEqual(set(headers), {"profile-web-page-url", "announce"})
        self.assertNotIn("support-url", headers)

    def test_announcements_point_to_each_apps_actual_control_and_preserve_bypass_notice(self):
        module = self._header_generator()
        for client in ("happ", "incy", "both"):
            headers = module.build_headers("https://wayspn.ru", client=client)
            announce = headers["announce"].removeprefix("rwEncodeBase64:")
            self.assertLessEqual(len(announce), 200)
            self.assertIn(module.BYPASS_NOTICE, announce)
            self.assertIn("\n", announce)
            self.assertEqual(headers["profile-web-page-url"], "https://wayspn.ru/renew#{{SUBSCRIPTION_URL}}")
            self.assertNotIn("announce-url", headers)
            if client == "happ":
                self.assertIn("ⓘ слева от трафика", announce)
                self.assertNotIn("🌐 Сайт", announce)
                self.assertNotIn("announce-url", headers)
            elif client == "incy":
                self.assertIn("«🌐 Сайт»", announce)
                self.assertNotIn("ⓘ", announce)
            else:
                self.assertIn("Happ — ⓘ", announce)
                self.assertIn("INCY — «🌐 Сайт»", announce)

    def test_prepared_header_files_match_the_free_generator(self):
        module = self._header_generator()
        folder = Path(__file__).resolve().parents[1] / "ops" / "subscription-renewal"
        for client, filename in (("both", "response-headers.json"), ("happ", "response-headers-happ.json"),
                                 ("incy", "response-headers-incy.json")):
            with self.subTest(client=client):
                self.assertEqual(json.loads((folder / filename).read_text()),
                                 module.build_headers("https://wayspn.ru", client=client))

    def test_free_cli_does_not_require_provider_id_or_change_support(self):
        module = self._header_generator()
        output = io.StringIO()
        with patch("sys.argv", ["prepare_happ_renewal.py", "--site-url", "https://wayspn.ru"]), redirect_stdout(output):
            module.main()
        self.assertEqual(json.loads(output.getvalue()), module.build_headers("https://wayspn.ru"))

    def test_metadata_generator_rejects_wrong_site_client_and_support(self):
        module = self._header_generator()
        for bad in ("http://wayspn.ru", "https://user@wayspn.ru", "https://wayspn.ru/account", "https://wayspn.ru#secret"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                module.build_headers(bad)
        with self.assertRaises(ValueError):
            module.build_headers("https://wayspn.ru", client="unknown")
        with self.assertRaises(ValueError):
            module.build_headers("https://wayspn.ru", "http://t.me/wayspn_support")
        headers = module.build_headers("https://wayspn.ru", "https://t.me/wayspn_support")
        self.assertEqual(headers["support-url"], "https://t.me/wayspn_support")

    async def test_yookassa_retry_keeps_one_key(self):
        class Context:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False

        class RemoteResponse(Context):
            status = 201
            async def json(self):
                return {"id": INVOICE_ID}

        keys = []
        class Session(Context):
            def post(self, url, **kwargs):
                keys.append(kwargs["headers"]["Idempotence-Key"])
                return RemoteResponse()

        async def retry_twice(callback, **kwargs):
            await callback()
            return await callback()

        with (
            patch.object(yookassa, "safe_api_call", new=retry_twice),
            patch.object(yookassa.aiohttp, "ClientSession", return_value=Session()),
            patch.object(yookassa.aiohttp, "TCPConnector"),
        ):
            await yookassa.create_yookassa_payment(None, 200, "regular_1m", 123, return_url="https://wayspn.ru/renew")
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])


if __name__ == "__main__":
    unittest.main()
