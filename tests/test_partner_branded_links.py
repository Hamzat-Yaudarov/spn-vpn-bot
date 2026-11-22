"""One branded URL in browser/native clients; all external ports are fake."""

import base64
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import FastAPI

from partner_subscription_web import create_subscription_router
from partner_public_checkout_web import create_router as create_checkout_router
from services.partner_subscriptions import (
    PartnerLinkSettings,
    PartnerSubscriptionLinks,
    PartnerSubscriptionService,
)
from test_partner_subscriptions import Store, KEY, NOW, REMOTE, observation, payload
from test_partner_public_checkout_web import Service as CheckoutFixture


class BrandedLinkTests(unittest.TestCase):
    def test_short_style_keeps_signature_and_legacy_format_explicit(self):
        row = Store().row
        legacy = PartnerSubscriptionLinks(PartnerLinkSettings("sub.wayspn.online", KEY))
        short = PartnerSubscriptionLinks(
            PartnerLinkSettings("sub.wayspn.online", KEY, short_links=True)
        )
        token = legacy.token(row)
        self.assertEqual(token, short.token(row))
        self.assertEqual(short.url(row), "https://my-vpn.sub.wayspn.online/" + token)
        self.assertEqual(short.import_url(row), short.url(row))
        self.assertEqual(short.url(row, config=True), short.url(row) + "/config")
        self.assertEqual(legacy.import_url(row), legacy.url(row) + "/config")
        with self.assertRaises(ValueError):
            PartnerLinkSettings("sub.wayspn.online", KEY, short_links=1)


class BrandedGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        self.links = PartnerSubscriptionLinks(
            PartnerLinkSettings("sub.wayspn.online", KEY, short_links=True)
        )
        self.provisioner, self.source = AsyncMock(), AsyncMock()
        self.provisioner.read_public_usage.return_value = observation()
        self.source.fetch.return_value = payload()
        self.service = PartnerSubscriptionService(
            self.store,
            self.links,
            provisioner=self.provisioner,
            source=self.source,
            enabled=True,
            clock=lambda: NOW,
        )
        self.token = self.links.token(self.store.row)
        self.path = "/" + self.token
        app = FastAPI()
        app.include_router(
            create_subscription_router(
                self.service, public_checkout=SimpleNamespace(enabled=True)
            )
        )
        self.client = await self.enterAsyncContext(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://my-vpn.sub.wayspn.online",
            )
        )

    async def test_browser_branded_page_and_same_url_for_copy_happ_and_renewal(self):
        response = await self.client.get(
            self.path, headers={"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("Моя подписка", response.text)
        self.assertIn('value="' + self.links.url(self.store.row) + '"', response.text)
        self.assertIn(
            "happ://add/" + quote(self.links.url(self.store.row), safe=""),
            response.text,
        )
        self.assertIn('data-purchase-path="' + self.path + '"', response.text)
        for private in [REMOTE, "PrivateCredential9", "telegramId", "tg_id"]:
            self.assertNotIn(private, response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertIn("Accept", response.headers["vary"])
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.source.fetch.assert_not_awaited()

    async def test_happ_happ_plus_incy_receive_config_not_page_with_real_identity(self):
        for ua in ["Happ/3.0", "Happ Plus/1.0", "HappPlus/2.0", "INCY/1.2"]:
            with self.subTest(ua=ua):
                self.source.fetch.reset_mock()
                response = await self.client.get(
                    self.path,
                    headers={
                        "accept": "text/html",
                        "user-agent": ua,
                        "x-hwid": "ActualDevice12345",
                        "x-device-os": "iOS",
                        "x-device-model": "iPhone",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, payload().body)
                forwarded = self.source.fetch.await_args.args[1]
                self.assertEqual(forwarded["user-agent"], ua)
                self.assertEqual(forwarded["x-hwid"], "ActualDevice12345")
                self.assertNotIn("accept", forwarded)
                self.assertEqual(
                    response.headers["profile-web-page-url"],
                    self.links.url(self.store.row),
                )
                title = response.headers["profile-title"].removeprefix("base64:")
                self.assertEqual(base64.b64decode(title).decode(), "Моя подписка")

    async def test_generic_native_star_or_json_requests_are_unchanged_config(self):
        for accept in [
            "*/*",
            "application/json",
            "text/plain",
            "text/html;q=0",
            "application/json,text/html;q=0.5",
        ]:
            with self.subTest(accept=accept):
                response = await self.client.get(self.path, headers={"accept": accept})
                self.assertEqual(response.content, payload().body)

    async def test_browser_navigation_even_inside_happ_webview_opens_html(self):
        response = await self.client.get(
            self.path,
            headers={
                "accept": "text/html",
                "user-agent": "Happ/3.0",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
            },
        )
        self.assertIn("text/html", response.headers["content-type"])
        self.source.fetch.assert_not_awaited()

    async def test_explicit_config_and_previous_urls_remain_compatible_without_redirect(
        self,
    ):
        for path in [self.path + "/config", "/s" + self.path + "/config"]:
            response = await self.client.get(path, headers={"accept": "text/html"})
            self.assertEqual(response.content, payload().body)
            self.assertNotIn("location", response.headers)
        response = await self.client.get("/s" + self.path)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertNotIn("location", response.headers)

    async def test_unauthorized_host_token_query_forwarded_host_do_not_bypass_capability(
        self,
    ):
        for path, headers in [
            (self.path, {"host": "other.sub.wayspn.online"}),
            (
                self.path,
                {
                    "host": "attacker.test",
                    "x-forwarded-host": "my-vpn.sub.wayspn.online",
                },
            ),
            (self.path + "?tg_id=123", {}),
            ("/" + "a" * 64, {}),
        ]:
            response = await self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 404)
        self.source.fetch.assert_not_awaited()

    async def test_ambiguous_headers_fail_closed_never_fallback_to_source(self):
        for headers in [
            [("accept", "text/html"), ("accept", "application/json")],
            {"accept": "text/html;q=nan"},
        ]:
            response = await self.client.get(self.path, headers=headers)
            self.assertEqual(response.status_code, 400)
        self.source.fetch.assert_not_awaited()

    async def test_expired_or_pending_config_denied_but_browser_can_read_status(self):
        self.store.row["expires_at"] = NOW - timedelta(seconds=1)
        self.store.row["desired_expires_at"] = NOW - timedelta(seconds=1)
        response = await self.client.get(self.path)
        self.assertEqual(response.status_code, 410)
        response = await self.client.get(self.path, headers={"accept": "text/html"})
        self.assertIn("Срок подписки истёк", response.text)
        self.source.fetch.assert_not_awaited()

    async def test_canonical_catchall_does_not_turn_assets_or_unknown_paths_into_subscription(
        self,
    ):
        for path in [
            "/robots.txt",
            "/favicon.ico",
            "/partner-subscription-assets/not-an-asset.js",
        ]:
            self.assertEqual((await self.client.get(path)).status_code, 404)
        self.source.fetch.assert_not_awaited()


class BrandedRenewalRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_routes_keep_origin_guard_and_legacy_alias(self):
        service = CheckoutFixture()
        app = FastAPI()
        app.include_router(create_checkout_router(service, enabled=True))
        key = str(uuid4())
        path = "/" + "a" * 64
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://own-brand.partners.example.test",
        ) as client:
            self.assertEqual(
                (await client.get(path + "/purchase-quote")).status_code, 200
            )
            self.assertEqual(
                (await client.get(path + "/purchases/" + key)).status_code, 200
            )
            self.assertEqual([call[0] for call in service.calls], ["quote", "get"])
            body = {"request_key": key, "expected_price_kopecks": 19900}
            self.assertEqual(
                (await client.post(path + "/purchases", json=body)).status_code, 403
            )
            self.assertEqual(
                (
                    await client.post(
                        path + "/purchases",
                        json=body,
                        headers={"origin": "https://own-brand.partners.example.test"},
                    )
                ).status_code,
                200,
            )
            self.assertEqual(service.calls[-1][1][0], "own-brand")
            self.assertEqual(
                (await client.get("/s" + path + "/purchase-quote")).status_code, 200
            )
