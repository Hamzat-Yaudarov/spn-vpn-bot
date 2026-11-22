"""Offline public renewal HTTP host/origin/capability request boundaries."""

from types import SimpleNamespace
import unittest
from uuid import uuid4

from fastapi import FastAPI
import httpx

from partner_public_checkout_web import create_router
from services.partner_subscriptions import PartnerSubscriptionLinks, PartnerLinkSettings


class Service:
    def __init__(self):
        self.links = PartnerSubscriptionLinks(
            PartnerLinkSettings(
                "partners.example.test", b"public-renewal-http-test-key-32bytes"
            )
        )
        self.calls = []

    async def quote(self, *args):
        self.calls.append(("quote", args))
        return {"price_kopecks": 19900, "renewal_available": True}

    async def create_purchase(self, *args):
        self.calls.append(("create", args))
        return {"status": "created", "request_key": args[2]}

    async def get_purchase(self, *args):
        self.calls.append(("get", args))
        return {"status": "created", "request_key": args[2]}


class PublicCheckoutHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = Service()
        app = FastAPI()
        app.include_router(create_router(self.service, enabled=True))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://own-brand.partners.example.test",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.path = "/s/" + "a" * 64
        self.body = {"request_key": str(uuid4()), "expected_price_kopecks": 19900}
        self.origin = {"origin": "https://own-brand.partners.example.test"}

    async def test_same_origin_only_and_cookie_or_tg_fields_not_identity(self):
        for headers in (
            {},
            {"origin": "null"},
            {"origin": "https://foreign.test"},
            {"origin": "http://own-brand.partners.example.test"},
            {"origin": "https://own-brand.partners.example.test/"},
            self.origin | {"sec-fetch-site": "cross-site"},
        ):
            result = await self.client.post(
                self.path + "/purchases", headers=headers, json=self.body
            )
            self.assertEqual(result.status_code, 403)
            self.assertIn("no-store", result.headers["cache-control"])
        self.assertEqual(self.service.calls, [])
        result = await self.client.post(
            self.path + "/purchases",
            headers=self.origin | {"cookie": "tg_id=999; admin=true"},
            json=self.body,
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.service.calls[0][1][0], "own-brand")
        result = await self.client.post(
            self.path + "/purchases",
            headers=self.origin,
            json=self.body | {"tg_id": 999},
        )
        self.assertEqual(result.status_code, 400)

    async def test_host_cannot_be_selected_with_forwarded_headers(self):
        result = await self.client.get(
            self.path + "/purchase-quote",
            headers={
                "host": "attacker.test",
                "x-forwarded-host": "own-brand.partners.example.test",
            },
        )
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.service.calls, [])
        result = await self.client.get(self.path + "/purchase-quote?tg_id=100")
        self.assertEqual(result.status_code, 404)

    async def test_get_quote_and_state_do_not_call_creation(self):
        quote = await self.client.get(self.path + "/purchase-quote")
        state = await self.client.get(
            self.path + "/purchases/" + self.body["request_key"]
        )
        self.assertEqual((quote.status_code, state.status_code), (200, 200))
        self.assertEqual([call[0] for call in self.service.calls], ["quote", "get"])
        self.assertEqual(state.headers["referrer-policy"], "no-referrer")

    async def test_strict_json_size_duplicates_unknown_fields(self):
        headers = self.origin | {"content-type": "application/json"}
        for content, status in (
            (b"[]", 400),
            (b'{"request_key":"a","request_key":"b"}', 400),
            (b'{"x":"' + b"a" * 5000 + b'"}', 413),
            (b'{"status":"paid"}', 400),
        ):
            result = await self.client.post(
                self.path + "/purchases", headers=headers, content=content
            )
            self.assertEqual(result.status_code, status)
        self.assertEqual(self.service.calls, [])

    async def test_disabled_router_does_not_call_service(self):
        app = FastAPI()
        app.include_router(create_router(self.service))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://own-brand.partners.example.test",
        ) as client:
            result = await client.get(self.path + "/purchase-quote")
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.service.calls, [])
