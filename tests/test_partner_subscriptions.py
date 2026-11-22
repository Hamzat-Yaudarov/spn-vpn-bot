"""Local capability, quota, source-transport and HTTP tests. No real network."""

from contextlib import asynccontextmanager
import copy
from datetime import timedelta
import json
import unittest
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from multidict import CIMultiDict

from partner_subscription_web import create_subscription_router
from services.partner_commerce_store import CommerceNotFound
from services.partner_subscriptions import (
    PartnerLinkSettings,
    PartnerSubscriptionLinks,
    PartnerSubscriptionService,
    PartnerSubscriptionSource,
    SubscriptionNotFound,
    SubscriptionUnavailable,
    SubscriptionPayload,
    normalized_quota,
    client_headers,
    MAX_CONFIG_BYTES,
)
from services.partner_traffic import PartnerTrafficUsage
from test_partner_remnawave import NOW, Factory, Response as RemoteResponse

GB = 1024**3
PID, SID, REMOTE = map(str, (uuid4(), uuid4(), uuid4()))
KEY = b"local-test-only-unique-signing-key-32"


def context(**changes):
    return {
        "project_id": PID,
        "subscription_id": SID,
        "slug": "my-vpn",
        "title": "Мой VPN",
        "brand": {"subscription_title": "Моя подписка", "accent_color": "#d8b55b"},
        "bot_username": "MyPartnerBot",
        "support_url": "https://t.me/MySupportBot",
        "expires_at": NOW + timedelta(days=30),
        "desired_expires_at": NOW + timedelta(days=30),
        "traffic_limit_bytes": 700 * GB,
        "traffic_cycle_baseline_bytes": 350 * GB,
        "device_limit": 3,
        "traffic_last_used_bytes": 350 * GB,
        "traffic_cycle_hold": None,
        "activation_pending": False,
        "remote_user_id": REMOTE,
        "remote_username": "wl_" + UUID(SID).hex,
        "subscription_url": "https://subscriptions.example.test/sub/PrivateCredential9",
        "public_link_generation": 0,
        **changes,
    }


def observation(row=None, **changes):
    row = row or context()
    return PartnerTrafficUsage(
        **{
            "project_id": row["project_id"],
            "subscription_id": row["subscription_id"],
            "remote_user_id": row["remote_user_id"],
            "used_bytes": 400 * GB,
            "limit_bytes": row["traffic_limit_bytes"],
            "observed_at": NOW,
            **changes,
        }
    )


def payload(row=None, **changes):
    row = row or context()
    return SubscriptionPayload(
        **{
            "body": b'{"outbounds":[{"tag":"immutable-transport"}]}',
            "content_type": "application/json",
            "headers": {
                "subscription-userinfo": f'upload=0; download={402*GB}; total={row["traffic_limit_bytes"]}; expire={int(row["expires_at"].timestamp())}',
                "profile-title": "PRIVATE MAIN TITLE",
                "support-url": "https://wrong.example.test",
                "set-cookie": "private-cookie",
                "profile-update-interval": "12",
                "x-hwid-active": "true",
            },
            **changes,
        }
    )


class Store:
    traffic_cycles_enabled = True

    def __init__(self):
        self.row, self.lookups, self.locks = context(), [], []

    async def resolve_public_subscription(self, slug, sub):
        self.lookups.append((slug, sub))
        if slug != self.row["slug"] or sub != self.row["subscription_id"]:
            raise CommerceNotFound()
        return copy.deepcopy(self.row)

    async def remember_public_usage(self, context, observation, *, now):
        if (
            self.row["public_link_generation"] != context["public_link_generation"]
            or self.row["activation_pending"]
        ):
            raise SubscriptionUnavailable()
        normalized_quota(self.row, observation, now)
        self.row["traffic_last_used_bytes"] = observation.used_bytes

    @asynccontextmanager
    async def acquire_activation_lock(self, project, sub):
        self.locks.append((project, sub))
        yield self


class LinkAndQuotaTests(unittest.TestCase):
    def setUp(self):
        self.links = PartnerSubscriptionLinks(
            PartnerLinkSettings("partners.example.test", KEY)
        )

    def test_secret_hidden_and_settings_strict(self):
        self.assertNotIn(KEY.decode(), repr(self.links.settings))
        self.assertEqual(
            PartnerLinkSettings("EXAMPLE.TEST", KEY).base_domain, "example.test"
        )
        for domain in (
            "https://example.test",
            "example.test/path",
            "localhost",
            "*.example.test",
        ):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                PartnerLinkSettings(domain, KEY)
        for key in (b"short", "text-not-bytes", b"x" * 65):
            with self.assertRaises(ValueError):
                PartnerLinkSettings("partners.example.test", key)

    def test_capability_stable_bound_to_project_host_generation_and_key(self):
        token = self.links.token(context())
        self.assertEqual(len(token), 64)
        self.assertEqual(self.links.subscription_id(token), SID)
        self.assertEqual(token, self.links.token(context()))
        self.links.verify(token, context())
        for changes in (
            {"project_id": str(uuid4())},
            {"subscription_id": str(uuid4())},
            {"slug": "other-vpn"},
            {"public_link_generation": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(SubscriptionNotFound):
                self.links.verify(token, context(**changes))
        other = PartnerSubscriptionLinks(PartnerLinkSettings("other.example.test", KEY))
        with self.assertRaises(SubscriptionNotFound):
            other.verify(token, context())
        other = PartnerSubscriptionLinks(
            PartnerLinkSettings("partners.example.test", b"x" * 32)
        )
        with self.assertRaises(SubscriptionNotFound):
            other.verify(token, context())

    def test_token_strict_canonical_format(self):
        for token in (
            None,
            SID,
            "_" * 63,
            "=" * 64,
            self.links.token(context()) + "=",
            "A" * 64,
        ):
            with self.assertRaises(SubscriptionNotFound):
                self.links.verify(token, context())

    def test_host_exact_one_level_only_no_forwarded_or_port_injection(self):
        self.assertEqual(
            self.links.slug_from_host("MY-VPN.partners.example.test:443"), "my-vpn"
        )
        for host in (
            "partners.example.test",
            "my-vpn.partners.example.test.evil.test",
            "x.y.partners.example.test",
            "my-vpn.partners.example.test:444",
            "my-vpn.partners.example.test.",
            "my-vpn.partners.example.test@evil.test",
            " my-vpn.partners.example.test",
            "admin.partners.example.test",
            "my-vpn..partners.example.test",
            "127.0.0.1",
        ):
            with self.subTest(host=host), self.assertRaises(SubscriptionNotFound):
                self.links.slug_from_host(host)

    def test_cumulative_cap_normalized_preserving_exact_remaining(self):
        quota = normalized_quota(context(), observation(), NOW)
        self.assertEqual(
            (quota["used_bytes"], quota["total_bytes"], quota["remaining_bytes"]),
            (50 * GB, 350 * GB, 300 * GB),
        )
        quota = normalized_quota(context(), observation(used_bytes=701 * GB), NOW)
        self.assertEqual(
            quota["remaining_bytes"], 0
        )  # Real overshoot is not an invented unlimited allowance.

    def test_missing_stale_rolled_back_foreign_or_unreviewed_counter_never_becomes_zero(
        self,
    ):
        for changes in (
            {"traffic_cycle_baseline_bytes": None},
            {"traffic_cycle_baseline_bytes": True},
            {"traffic_cycle_hold": "legacy_review"},
            {"traffic_cycle_baseline_bytes": 700 * GB},
        ):
            with self.subTest(changes=changes), self.assertRaises(
                (ValueError, SubscriptionUnavailable)
            ):
                normalized_quota(context(**changes), observation(), NOW)
        for changes in (
            {"used_bytes": None},
            {"used_bytes": 349 * GB},
            {"used_bytes": True},
            {"observed_at": NOW - timedelta(seconds=61)},
            {"observed_at": NOW + timedelta(seconds=1)},
            {"project_id": str(uuid4())},
            {"subscription_id": str(uuid4())},
            {"remote_user_id": "foreign"},
            {"limit_bytes": 699 * GB},
        ):
            with self.subTest(changes=changes), self.assertRaises(
                (ValueError, SubscriptionUnavailable)
            ):
                normalized_quota(context(), observation(**changes), NOW)

    def test_only_real_device_headers_forwarded_without_secret_or_identity_forgery(
        self,
    ):
        result = client_headers(
            {
                "User-Agent": "INCY/1.0",
                "x-hwid": "MyActualDevice123",
                "x-device-os": "iOS",
                "Authorization": "Bearer secret",
                "Cookie": "secret",
                "Host": "evil.test",
                "X-Forwarded-For": "10.0.0.1",
            }
        )
        self.assertEqual(set(result), {"user-agent", "x-hwid", "x-device-os"})
        for value in ("short", "x" * 65, "xxxxxxxxxx_secret", "fake\r\nHost:evil"):
            with self.assertRaises(ValueError):
                client_headers({"x-hwid": value})
        self.assertNotIn("x-hwid", client_headers({}))  # Never manufacture a HWID.


class GatewayHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        self.links = PartnerSubscriptionLinks(
            PartnerLinkSettings("partners.example.test", KEY)
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
        app = FastAPI()
        app.include_router(create_subscription_router(self.service))
        self.client = await self.enterAsyncContext(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://my-vpn.partners.example.test",
            )
        )
        self.page = "/s/" + self.token

    async def test_page_brand_safe_no_source_identity_or_upstream_request(self):
        response = await self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Моя подписка", response.text)
        self.assertIn("Подписка активна", response.text)
        self.assertIn("300 ГБ", response.text)
        self.assertIn("из 350 ГБ", response.text)
        self.assertNotIn("700 ГБ", response.text)
        self.assertNotIn("PrivateCredential9", response.text)
        self.assertNotIn(REMOTE, response.text)
        self.assertIn("happ://add/https%3A", response.text)
        self.assertNotIn("incy://", response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("connect-src 'none'", response.headers["content-security-policy"])
        self.source.fetch.assert_not_awaited()  # Opening HTML must not register an extra device.

    async def test_config_preserves_payload_and_correct_headers_not_upstream_brand_or_cookies(
        self,
    ):
        response = await self.client.get(
            self.page + "/config",
            headers={"user-agent": "Happ/3.0", "x-hwid": "MyActualDevice123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, payload().body)
        self.assertEqual(
            response.headers["subscription-userinfo"],
            f"upload=0; download={52*GB}; total={350*GB}; expire={int(context()['expires_at'].timestamp())}",
        )
        self.assertEqual(
            response.headers["profile-web-page-url"], self.links.url(context())
        )
        self.assertEqual(response.headers["support-url"], "https://t.me/MySupportBot")
        self.assertEqual(response.headers["profile-update-interval"], "12")
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn("PRIVATE MAIN", response.headers["profile-title"])
        self.assertEqual(response.headers["x-hwid-active"], "true")
        args = self.source.fetch.await_args.args
        self.assertEqual(args[0], context()["subscription_url"])
        self.assertEqual(
            args[1], {"user-agent": "Happ/3.0", "x-hwid": "MyActualDevice123"}
        )
        self.assertTrue(self.store.locks)

    async def test_forged_revoked_foreign_unknown_link_same_not_found(self):
        for path, host in (
            (self.page, "other-vpn.partners.example.test"),
            (self.page, "evil.test"),
            (self.page + "?url=https://evil.test", None),
            ("/s/" + "A" * 64, None),
            ("/s/" + self.token[:-1] + ("X" if self.token[-1] != "X" else "Y"), None),
        ):
            response = await self.client.get(
                path, headers={"host": host} if host else {}
            )
            self.assertEqual(response.status_code, 404)
        self.store.row["public_link_generation"] = 1
        self.assertEqual((await self.client.get(self.page)).status_code, 404)
        self.provisioner.read_public_usage.assert_not_awaited()
        self.source.fetch.assert_not_awaited()

    async def test_duplicate_and_forwarded_hosts_do_not_authorize_unknown_tenant(self):
        response = await self.client.get(
            self.page,
            headers=[
                ("host", "evil.test"),
                ("x-forwarded-host", "my-vpn.partners.example.test"),
            ],
        )
        self.assertEqual(response.status_code, 404)
        response = await self.client.get(
            self.page,
            headers=[
                ("host", "my-vpn.partners.example.test"),
                ("host", "my-vpn.partners.example.test"),
            ],
        )
        self.assertEqual(response.status_code, 404)

    async def test_loading_pending_expired_and_error_never_fake_zero_or_allow_config(
        self,
    ):
        for change, message, code in (
            ({"activation_pending": True}, "Оплата сохранена", 503),
            ({"expires_at": NOW}, "Срок подписки истёк", 410),
            (
                {"traffic_cycle_hold": "counter_changed"},
                "Данные временно недоступны",
                503,
            ),
            ({"traffic_cycle_baseline_bytes": None}, "Данные временно недоступны", 503),
        ):
            self.store.row = context(**change)
            page = await self.client.get(self.page)
            self.assertEqual(page.status_code, 200)
            self.assertIn(message, page.text)
            self.assertNotIn('id="copy-link"', page.text)
            self.assertNotIn("Осталось трафика", page.text)
            self.assertEqual(
                (await self.client.get(self.page + "/config")).status_code, code
            )
        self.source.fetch.assert_not_awaited()

    async def test_remote_failure_no_raw_token_no_payment_suggestion_and_no_zero(self):
        self.provisioner.read_public_usage.side_effect = RuntimeError(
            "PRIVATE API TOKEN"
        )
        response = await self.client.get(self.page)
        self.assertIn("Данные временно недоступны", response.text)
        self.assertNotIn("PRIVATE", response.text)
        self.assertEqual(
            (await self.client.get(self.page + "/config")).status_code, 503
        )
        self.source.fetch.assert_not_awaited()

    async def test_verified_metadata_missing_wrong_cap_expiry_duplicate_rejected(self):
        for value in (
            "",
            "total=0",
            payload()
            .headers["subscription-userinfo"]
            .replace(str(700 * GB), str(701 * GB)),
            payload().headers["subscription-userinfo"] + "; total=1",
            payload().headers["subscription-userinfo"].replace("expire=", "other="),
        ):
            headers = {**payload().headers, "subscription-userinfo": value}
            self.source.fetch.return_value = payload(headers=headers)
            self.assertEqual(
                (await self.client.get(self.page + "/config")).status_code, 503
            )

    async def test_hwid_denial_flags_propagate_no_fabricated_config(self):
        for status in (200, 404):
            self.source.fetch.return_value = SubscriptionPayload(
                b"", "text/plain", {"x-hwid-not-supported": "true"}, status
            )
            response = await self.client.get(self.page + "/config")
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.content, b"")
            self.assertEqual(response.headers["x-hwid-not-supported"], "true")

    async def test_rotation_during_fetch_or_new_paid_snapshot_rejects_old_response(
        self,
    ):
        async def fetch(*args):
            self.store.row["public_link_generation"] = 1
            return payload()

        self.source.fetch.side_effect = fetch
        self.assertEqual(
            (await self.client.get(self.page + "/config")).status_code, 404
        )

        async def new_order(*args):
            self.store.row["activation_pending"] = True
            return payload()

        self.store.row = context()
        self.source.fetch.side_effect = new_order
        self.assertEqual(
            (await self.client.get(self.page + "/config")).status_code, 503
        )

    async def test_brand_escape_contrast_no_scripts_or_external_image_fetch(self):
        self.store.row["title"] = '<script>alert("x")</script>'
        self.store.row["brand"] = {
            "subscription_description": '<img src=x onerror="alert(1)">',
            "accent_color": "#000000",
            "logo_url": "https://tracker.example.test/pixel",
        }
        response = await self.client.get(self.page)
        self.assertIn("&lt;img", response.text)
        self.assertIn("--on-accent:#ffffff", response.text)
        self.assertNotIn("tracker.example.test", response.text)
        self.assertNotIn("<script>alert", response.text)

    async def test_oversized_duplicate_device_headers_rejected_before_source(self):
        for headers in (
            [("x-hwid", "MyActualDevice123"), ("x-hwid", "MyActualDevice456")],
            {"user-agent": "x" * 513},
            {"x-hwid": "notvalid"},
        ):
            response = await self.client.get(self.page + "/config", headers=headers)
            self.assertEqual(response.status_code, 400)
        self.source.fetch.assert_not_awaited()

    async def test_disabled_routes_no_database_network_and_private_asset_allowlist(
        self,
    ):
        response = await self.client.get(
            "/partner-subscription-assets/subscription.js?v=20261019-gateway6"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (
                await self.client.get("/partner-subscription-assets/secret.txt")
            ).status_code,
            404,
        )
        self.service.enabled = False
        for path in (
            self.page,
            self.page + "/config",
            "/partner-subscription-assets/subscription.css",
        ):
            self.assertEqual((await self.client.get(path)).status_code, 404)
        self.assertEqual(self.store.lookups, [])
        self.source.fetch.assert_not_awaited()


class SourceResponse(RemoteResponse):
    def __init__(
        self,
        *,
        status=200,
        raw=b"aW1tdXRhYmxl",
        content_type="text/plain",
        headers=None,
    ):
        super().__init__({}, status, raw)
        self.headers = CIMultiDict({"content-type": content_type, **(headers or {})})


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def source(self, responses):
        factory = Factory(responses)
        return (
            PartnerSubscriptionSource(
                "https://subscriptions.example.test", session_factory=factory
            ),
            factory,
        )

    async def test_inert_bound_to_fixed_origin_and_no_redirects_or_proxy_env(self):
        source, factory = self.source([SourceResponse()])
        self.assertEqual(factory.requests, [])
        response = await source.fetch(
            context()["subscription_url"],
            {"user-agent": "INCY/1.0", "Cookie": "private"},
        )
        self.assertEqual(response.body, b"aW1tdXRhYmxl")
        method, url, options = factory.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, context()["subscription_url"])
        self.assertFalse(options["allow_redirects"])
        self.assertNotIn("Cookie", options["headers"])
        self.assertFalse(factory.options[0]["trust_env"])
        self.assertEqual(factory.options[0]["timeout"].total, 12)

    async def test_ssrf_query_path_credentials_ip_and_saved_origin_mismatch_never_requested(
        self,
    ):
        for url in (
            "https://127.0.0.1/sub/abcdefghi",
            "http://subscriptions.example.test/sub/abcdefghi",
            "https://evil.test/sub/abcdefghi",
            "https://subscriptions.example.test.evil.test/sub/abcdefghi",
            "https://subscriptions.example.test/api/users",
            "https://subscriptions.example.test/sub/a%2fb",
            "https://u:p@subscriptions.example.test/sub/abcdefghi",
            "https://subscriptions.example.test/sub/abcdefghi?format=x",
            "https://subscriptions.example.test:444/sub/abcdefghi",
            "https://subscriptions.example.test/sub/abcdefghi#x",
        ):
            source, factory = self.source([])
            with self.subTest(url=url), self.assertRaises(SubscriptionUnavailable):
                await source.fetch(url, {})
            self.assertEqual(factory.requests, [])

    async def test_redirects_html_errors_oversize_empty_and_duplicate_headers_rejected(
        self,
    ):
        duplicate = SourceResponse()
        duplicate.headers.add("subscription-userinfo", "total=1")
        duplicate.headers.add("subscription-userinfo", "total=2")
        for response in (
            SourceResponse(status=302),
            SourceResponse(status=500),
            SourceResponse(status=404),
            SourceResponse(content_type="text/html", raw=b"<html>secret</html>"),
            SourceResponse(raw=b"x" * (MAX_CONFIG_BYTES + 1)),
            SourceResponse(raw=b""),
            duplicate,
            SourceResponse(headers={"routing": "x\r\nset-cookie:secret"}),
            TimeoutError("PRIVATE SOURCE URL"),
        ):
            source, factory = self.source([response])
            with self.assertRaises(SubscriptionUnavailable) as error:
                await source.fetch(context()["subscription_url"], {})
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertEqual(len(factory.requests), 1)

    async def test_json_yaml_uri_base64_byte_identical_without_unknown_response_headers(
        self,
    ):
        for media, body in (
            ("application/json", b'{"outbounds":[]}'),
            ("application/x-yaml", b"proxies: []\n"),
            ("text/plain", b"vless://unchanged"),
            ("text/plain", b"dmxlc3M6Ly91bmNoYW5nZWQ="),
        ):
            source, _ = self.source(
                [
                    SourceResponse(
                        raw=body, content_type=media, headers={"Set-Cookie": "secret"}
                    )
                ]
            )
            result = await source.fetch(context()["subscription_url"], {})
            self.assertEqual(result.body, body)
            self.assertNotIn("set-cookie", result.headers)

    async def test_hwid_denial_never_passes_error_html_as_credentials(self):
        source, _ = self.source(
            [
                SourceResponse(
                    status=404,
                    raw=b"<html>private upstream error</html>",
                    content_type="text/html",
                    headers={"x-hwid-max-devices-reached": "true"},
                )
            ]
        )
        result = await source.fetch(context()["subscription_url"], {})
        self.assertEqual(
            (result.status, result.body, result.content_type), (404, b"", "text/plain")
        )
