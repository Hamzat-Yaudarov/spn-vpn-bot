"""Partner provider contract and transport checks; no real merchant/network."""
import asyncio
import json
import unittest
from uuid import uuid4

from services.partner_checkout import CheckoutError
from services.partner_yookassa import PartnerYooKassaProvider, PartnerYooKassaSettings


SHOP = "12345"
SECRET = "local-test-secret-never-real"
ORDER = "8175b112-bc10-4468-a5d0-a0898d620db3"
PROJECT = "b6fb7a31-0ee5-492d-813c-086077cdb133"
PAYMENT = "local-invoice-1"


def payment_body():
    return {
        "id": PAYMENT, "recipient": {"account_id": SHOP}, "test": True,
        "paid": False, "status": "pending",
        "amount": {"value": "199.01", "currency": "RUB"},
        "metadata": {"wl_order_id": ORDER, "wl_project_id": PROJECT, "wl_tg_id": "42"},
        "confirmation": {"confirmation_url": "https://pay.example.test/invoice"},
    }


class Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


class Response:
    def __init__(self, body=None, *, chunks=None, status=200):
        raw = json.dumps(payment_body() if body is None else body).encode()
        self.status = status
        self.content = Content(chunks if chunks is not None else [raw[:11], raw[11:43], raw[43:]])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, factory):
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        self.factory.requests.append((method, url, kwargs))
        if self.factory.error:
            raise self.factory.error
        return self.factory.response


class Factory:
    def __init__(self, response=None, error=None):
        self.response = response or Response()
        self.error = error
        self.options = []
        self.requests = []

    def __call__(self, **kwargs):
        self.options.append(kwargs)
        return Session(self)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, response=None, error=None, **settings):
        factory = Factory(response, error)
        config = PartnerYooKassaSettings(SHOP, SECRET, "https://return.example.test/paid", **settings)
        return PartnerYooKassaProvider(config, session_factory=factory), factory

    def order(self, price=19901):
        return {"id": ORDER, "project_id": PROJECT, "tg_id": 42, "price_kopecks": price}

    async def test_constructor_has_no_network_and_repr_hides_secret(self):
        provider, factory = self.provider()
        self.assertEqual(factory.options, [])
        self.assertNotIn(SECRET, repr(provider))
        self.assertNotIn(SECRET, repr(provider.settings))
        self.assertTrue(provider.settings.test_mode)

    async def test_create_exact_kopecks_metadata_and_stable_idempotency(self):
        provider, factory = self.provider()
        first = await provider.create_invoice(self.order(), idempotence_key=ORDER)
        second = await provider.create_invoice(self.order(), idempotence_key=ORDER)
        self.assertEqual(first, second)
        self.assertEqual(first.amount_kopecks, 19901)
        self.assertEqual(first.order_id, ORDER)
        self.assertEqual(first.project_id, PROJECT)
        self.assertEqual(first.tg_id, 42)
        for method, url, options in factory.requests:
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://api.yookassa.ru/v3/payments")
            self.assertFalse(options["allow_redirects"])
            self.assertEqual(options["headers"]["Idempotence-Key"], ORDER)
            self.assertEqual(options["json"]["amount"], {"value": "199.01", "currency": "RUB"})
            self.assertEqual(options["json"]["metadata"], {"wl_order_id": ORDER, "wl_project_id": PROJECT,
                                                          "wl_tg_id": "42", "wl_kind": "subscription"})
            self.assertTrue(options["json"]["capture"])
            self.assertEqual(options["json"]["confirmation"], {"type": "redirect", "return_url": "https://return.example.test/paid"})
        for options in factory.options:
            self.assertFalse(options["trust_env"])
            self.assertEqual(options["timeout"].total, 20)
            self.assertEqual(options["timeout"].connect, 5)
            self.assertEqual(options["auth"].login, SHOP)

    async def test_provider_amount_is_exact_and_decimal_only(self):
        for invalid in (199.01, 19901, True, "199", "0199.01", "199.001", "1e2", "NaN", "-199.01", "0.00", "100000.01"):
            with self.subTest(value=invalid):
                body = payment_body()
                body["amount"]["value"] = invalid
                provider, _ = self.provider(Response(body))
                with self.assertRaises(CheckoutError):
                    await provider.get_payment(PAYMENT)

    async def test_entry_payment_has_distinct_purpose_and_exact_fixed_fee(self):
        body = payment_body()
        body["metadata"]["wl_kind"] = "entry"
        body["amount"]["value"] = "9990.00"
        provider, factory = self.provider(Response(body))
        order = {**self.order(999000), "purpose": "entry"}
        result = await provider.create_invoice(order, idempotence_key=ORDER)
        self.assertEqual(result.purpose, "entry")
        payload = factory.requests[0][2]["json"]
        self.assertEqual(payload["amount"], {"value": "9990.00", "currency": "RUB"})
        self.assertEqual(payload["metadata"]["wl_kind"], "entry")
        self.assertEqual(payload["description"], "Подключение партнёрского проекта")
        for invalid in (19900, 998999, 999001):
            with self.assertRaises(CheckoutError):
                await provider.create_invoice({**order, "price_kopecks": invalid}, idempotence_key=ORDER)
        self.assertEqual(len(factory.requests), 1)

    async def test_unknown_payment_purpose_cannot_issue_or_parse_invoice(self):
        provider, factory = self.provider()
        with self.assertRaises(CheckoutError):
            await provider.create_invoice({**self.order(), "purpose": "withdrawal"}, idempotence_key=ORDER)
        self.assertEqual(factory.requests, [])
        body = payment_body()
        body["metadata"]["wl_kind"] = "withdrawal"
        provider, _ = self.provider(Response(body))
        with self.assertRaises(CheckoutError):
            await provider.get_payment(PAYMENT)

    async def test_requested_amount_cannot_be_float_bool_or_below_minimum(self):
        provider, factory = self.provider()
        for invalid in (True, 19900.0, 19899, -1, 0, 10_000_001, "19900"):
            with self.subTest(value=invalid):
                with self.assertRaises(CheckoutError):
                    await provider.create_invoice(self.order(invalid), idempotence_key=ORDER)
        self.assertEqual(factory.requests, [])

    async def test_addon_price_below_subscription_minimum_requires_explicit_package(self):
        for purpose, quantity, price in (("traffic", 10, 2400), ("devices", 1, 1000)):
            body = payment_body()
            body["metadata"]["wl_kind"] = purpose
            body["amount"]["value"] = f"{price // 100}.{price % 100:02d}"
            provider, factory = self.provider(Response(body))
            order = {**self.order(price), "purpose": purpose, "addon_quantity": quantity,
                     "package_id": "gb_10" if purpose == "traffic" else "1", "subscription_id": str(uuid4())}
            result = await provider.create_invoice(order, idempotence_key=ORDER)
            self.assertEqual((result.purpose, result.amount_kopecks), (purpose, price))
            self.assertEqual(factory.requests[0][2]["json"]["metadata"]["wl_kind"], purpose)
            self.assertIn("Докупка", factory.requests[0][2]["json"]["description"])
            for changes in ({"addon_quantity": True}, {"addon_quantity": 0}, {"package_id": "../bad"},
                            {"subscription_id": "foreign-not-uuid"}, {"price_kopecks": 0}):
                with self.assertRaises(CheckoutError):
                    await provider.create_invoice({**order, **changes}, idempotence_key=ORDER)
            self.assertEqual(len(factory.requests), 1)

    async def test_idempotence_key_must_be_same_order(self):
        provider, factory = self.provider()
        with self.assertRaises(CheckoutError):
            await provider.create_invoice(self.order(), idempotence_key=str(uuid4()))
        self.assertEqual(factory.requests, [])

    async def test_customer_id_is_integer_not_bool_or_unbounded(self):
        provider, factory = self.provider()
        for tg_id in (True, 0, -1, "42", 2**63):
            order = self.order()
            order["tg_id"] = tg_id
            with self.assertRaises(CheckoutError):
                await provider.create_invoice(order, idempotence_key=ORDER)
        self.assertEqual(factory.requests, [])

    async def test_get_has_fixed_origin_no_redirect_and_validated_path(self):
        provider, factory = self.provider()
        result = await provider.get_payment(PAYMENT)
        self.assertEqual(result.id, PAYMENT)
        method, url, options = factory.requests[0]
        self.assertEqual((method, url), ("GET", "https://api.yookassa.ru/v3/payments/local-invoice-1"))
        self.assertFalse(options["allow_redirects"])
        self.assertNotIn("Idempotence-Key", options["headers"])
        for invalid in ("", "../payments", "https://evil.example/id", "local?token=x", "local#x", "x" * 129, 42):
            with self.assertRaises(CheckoutError):
                await provider.get_payment(invalid)
        self.assertEqual(len(factory.requests), 1)

    async def test_returned_payment_id_must_match_lookup(self):
        body = payment_body()
        body["id"] = "different-payment"
        provider, _ = self.provider(Response(body))
        with self.assertRaises(CheckoutError):
            await provider.get_payment(PAYMENT)

    async def test_wrong_merchant_and_testmode_rejected(self):
        for field, value in (("recipient", {"account_id": "99999"}), ("recipient", {"account_id": int(SHOP)}), ("test", False), ("test", 1)):
            body = payment_body()
            body[field] = value
            provider, _ = self.provider(Response(body))
            with self.assertRaises(CheckoutError):
                await provider.get_payment(PAYMENT)
        body = payment_body()
        body["test"] = False
        provider, _ = self.provider(Response(body), test_mode=False)
        self.assertEqual((await provider.get_payment(PAYMENT)).id, PAYMENT)

    async def test_bad_metadata_is_rejected_without_echo(self):
        for metadata in ({}, {"wl_order_id": "private-bad-order", "wl_project_id": PROJECT, "wl_tg_id": "42"},
                         {"wl_order_id": ORDER, "wl_project_id": "private-bad-project", "wl_tg_id": "42"},
                         {"wl_order_id": ORDER, "wl_project_id": PROJECT, "wl_tg_id": 42},
                         {"wl_order_id": ORDER, "wl_project_id": PROJECT, "wl_tg_id": "042"},
                         {"wl_order_id": ORDER, "wl_project_id": PROJECT, "wl_tg_id": "0"}):
            body = payment_body()
            body["metadata"] = metadata
            provider, _ = self.provider(Response(body))
            with self.assertRaises(CheckoutError) as caught:
                await provider.get_payment(PAYMENT)
            self.assertNotIn("private-bad", str(caught.exception))

    async def test_status_and_paid_types_are_not_coerced(self):
        for field, value in (("status", "refunded"), ("status", True), ("paid", 1), ("paid", "true")):
            body = payment_body()
            body[field] = value
            provider, _ = self.provider(Response(body))
            with self.assertRaises(CheckoutError):
                await provider.get_payment(PAYMENT)

    async def test_confirmation_link_is_https_without_credentials(self):
        for url in ("http://pay.example.test/invoice", "https://name:secret@pay.example.test", "https://pay.example.test:444", "javascript:alert(1)", "https://pay.example.test/\nprivate"):
            body = payment_body()
            body["confirmation"]["confirmation_url"] = url
            provider, _ = self.provider(Response(body))
            with self.assertRaises(CheckoutError) as caught:
                await provider.get_payment(PAYMENT)
            self.assertNotIn(url, str(caught.exception))

    async def test_completed_payment_may_have_no_confirmation_url(self):
        body = payment_body()
        body.update(status="succeeded", paid=True)
        del body["confirmation"]
        provider, _ = self.provider(Response(body))
        result = await provider.get_payment(PAYMENT)
        self.assertEqual(result.confirmation_url, "")
        self.assertTrue(result.paid)

    async def test_non_success_transport_status_and_redirect_are_redacted(self):
        for status in (301, 302, 307, 401, 429, 500):
            provider, _ = self.provider(Response({"secret": SECRET}, status=status))
            with self.assertRaises(CheckoutError) as caught:
                await provider.get_payment(PAYMENT)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    async def test_transport_exception_does_not_echo_credentials_or_private_url(self):
        provider, _ = self.provider(error=RuntimeError(f"{SECRET} https://private.example.test/credential"))
        with self.assertRaises(CheckoutError) as caught:
            await provider.get_payment(PAYMENT)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn("private.example", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_incomplete_nonobject_or_oversize_chunked_json_rejected(self):
        for chunks in ([b'{"id":'], [b"[]"], [b"null"], [b"not-json"], [b"x" * 8192] * 17):
            provider, _ = self.provider(Response(chunks=chunks))
            with self.assertRaises(CheckoutError):
                await provider.get_payment(PAYMENT)

    async def test_chunked_valid_response_is_read_to_eof(self):
        raw = json.dumps(payment_body()).encode()
        provider, _ = self.provider(Response(chunks=[raw[index:index + 1] for index in range(len(raw))], status=201))
        self.assertEqual((await provider.get_payment(PAYMENT)).amount_kopecks, 19901)


class SettingsTests(unittest.TestCase):
    def test_settings_require_explicit_merchant_secret_and_https_return(self):
        for shop, secret, url, mode in (("bad-shop", SECRET, "https://return.example.test", True),
                                       (SHOP, "short", "https://return.example.test", True),
                                       (SHOP, SECRET + "\n", "https://return.example.test", True),
                                       (SHOP, SECRET, "http://return.example.test", True),
                                       (SHOP, SECRET, "https://user:secret@return.example.test", True),
                                       (SHOP, SECRET, "https://return.example.test", 1)):
            with self.assertRaises(ValueError) as caught:
                PartnerYooKassaSettings(shop, secret, url, mode)
            self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
