"""Pure identities and mocked panel only; never production users or credentials."""

import copy
import unittest
from uuid import uuid4

from services.partner_panel_identity import (
    identity_fields,
    legacy_identity,
    readable_identity,
    validate_identity,
)
from services.partner_remnawave import PartnerProvisioningError
import test_partner_remnawave as fixtures
from test_partner_remnawave import (
    PROJECT,
    REMOTE,
    SNAPSHOT,
    SUB,
    absent,
    ok,
    user,
)


def readable():
    return readable_identity(
        PROJECT,
        SUB,
        123456789,
        slug="sever-vpn",
        title="Север VPN",
        bot_username="SeverBot",
    )


class PanelIdentityTests(unittest.TestCase):
    def test_frozen_human_identity_includes_old_exact_marker(self):
        identity = readable()
        self.assertEqual(identity["username"], "sever_vpn_" + SUB.replace("-", "")[:20])
        self.assertLessEqual(len(identity["username"]), 36)
        self.assertLessEqual(len(identity["tag"]), 16)
        self.assertRegex(identity["tag"], r"^[A-Z0-9_]+$")
        self.assertIn("Север VPN · @SeverBot", identity["description"])
        self.assertTrue(identity["description"].endswith(f"SPN-WL:{PROJECT}:{SUB}"))
        self.assertEqual(validate_identity(identity, PROJECT, SUB), identity)
        self.assertEqual(
            identity_fields(identity, PROJECT, SUB)["telegramId"], 123456789
        )
        other = readable_identity(
            PROJECT,
            uuid4(),
            123456790,
            slug="sever-vpn",
            title="Север VPN",
            bot_username="SeverBot",
        )
        self.assertEqual(other["tag"], identity["tag"])
        self.assertNotEqual(other["username"], identity["username"])

    def test_every_corruption_or_foreign_marker_fails_closed(self):
        identity = readable()
        for changes in (
            {"username": "other_123"},
            {"description": "Север VPN"},
            {"tag": "P_FOREIGN"},
            {"telegram_id": True},
            {"version": True},
            {"title": "Другое название"},
            {"bot_username": "OtherBot"},
            {"unexpected": "metadata"},
            {"version": 99},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_identity({**identity, **changes}, PROJECT, SUB)
        for pid, sid in ((uuid4(), SUB), (PROJECT, uuid4())):
            with self.assertRaises(ValueError):
                validate_identity(identity, pid, sid)
        for field, bad in (
            ("slug", "../foreign"),
            ("title", "VPN\nInjected"),
            ("bot_username", "https://t.me/Bad"),
        ):
            data = dict(slug="sever-vpn", title="Север VPN", bot_username="SeverBot")
            data[field] = bad
            with self.assertRaises(ValueError):
                readable_identity(PROJECT, SUB, 123, **data)

    def test_legacy_identity_is_exactly_old_marker_without_new_metadata(self):
        old = legacy_identity(PROJECT, SUB)
        self.assertEqual(
            identity_fields(old, PROJECT, SUB),
            {
                "username": "wl_" + SUB.replace("-", ""),
                "description": f"SPN-WL:{PROJECT}:{SUB}",
            },
        )

    def test_long_names_fit_panel_limits_and_telegram_id_is_exact_json_integer(self):
        value = readable_identity(
            PROJECT,
            SUB,
            2**53 - 1,
            slug="a-very-long-brand-name-123456789",
            title="Я" * 80,
            bot_username="B" * 32,
        )
        self.assertLessEqual(len(value["username"]), 36)
        self.assertLessEqual(len(value["tag"]), 16)
        self.assertLessEqual(len(value["description"]), 256)
        for tg_id in (True, 0, -1, 2**53):
            with self.assertRaises(ValueError):
                readable_identity(
                    PROJECT,
                    SUB,
                    tg_id,
                    slug="sever-vpn",
                    title="VPN",
                    bot_username="SeverBot",
                )


class ReadablePanelTransportTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, responses, *, version=2):
        return fixtures.RemnawaveTests().adapter(responses, version=version)

    def snapshot(self):
        identity = readable()
        return {
            **SNAPSHOT,
            "remote_identity": identity,
            "remote_username": identity["username"],
        }

    def remote(self, **changes):
        return user(**identity_fields(readable(), PROJECT, SUB), **changes)

    async def test_new_create_readback_and_unicode_description(self):
        remote = self.remote()
        adapter, transport = self.adapter([absent(), ok(remote), ok(remote)])
        result = await adapter.ensure_subscription(self.snapshot())
        payload = transport.requests[1][2]["json"]
        self.assertEqual(result.remote_username, readable()["username"])
        for key, value in identity_fields(readable(), PROJECT, SUB).items():
            self.assertEqual(payload[key], value)
        self.assertEqual([r[0] for r in transport.requests], ["GET", "POST", "GET"])

    async def test_unknown_create_recovers_frozen_name_no_second_post(self):
        remote = self.remote()
        adapter, transport = self.adapter(
            [absent(), TimeoutError(), ok(remote), ok(remote), ok(remote)]
        )
        with self.assertRaises(PartnerProvisioningError):
            await adapter.ensure_subscription(self.snapshot())
        result = await adapter.ensure_subscription(self.snapshot())
        self.assertEqual(result.remote_user_id, REMOTE)
        self.assertEqual(
            [r[0] for r in transport.requests], ["GET", "POST", "GET", "PATCH", "GET"]
        )

    async def test_v3_readable_identity_keeps_numeric_remote_id(self):
        remote = user(3, **identity_fields(readable(), PROJECT, SUB))
        adapter, transport = self.adapter(
            [ok(remote), ok(remote), ok(remote)], version=3
        )
        result = await adapter.ensure_subscription(
            {**self.snapshot(), "remote_user_id": "77"}
        )
        payload = transport.requests[1][2]["json"]
        self.assertEqual(result.remote_user_id, "77")
        self.assertEqual(payload["id"], 77)
        self.assertEqual(payload["telegramId"], 123456789)
        self.assertNotIn("uuid", payload)
        self.assertNotIn("password", payload)

    async def test_malformed_snapshot_never_falls_back_to_a_new_legacy_user(self):
        for identity in ({}, None, {"version": 2}):
            adapter, transport = self.adapter([])
            with self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription(
                    {**SNAPSHOT, "remote_identity": identity}
                )
            self.assertEqual(transport.requests, [])

    async def test_foreign_tag_telegram_marker_and_readback_never_take_over(self):
        for changes in (
            {"tag": "P_OTHER"},
            {"telegramId": 987},
            {"telegramId": "123456789"},
            {"description": "Север VPN"},
        ):
            remote = self.remote()
            remote.update(changes)
            adapter, transport = self.adapter([ok(remote)])
            with self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription(self.snapshot())
            self.assertEqual([r[0] for r in transport.requests], ["GET"])
        remote = self.remote()
        changed = copy.deepcopy(remote)
        changed["telegramId"] += 1
        adapter, _ = self.adapter([absent(), ok(remote), ok(changed)])
        with self.assertRaises(PartnerProvisioningError):
            await adapter.ensure_subscription(self.snapshot())

    async def test_public_usage_requires_same_snapshot_and_source_credential(self):
        remote = self.remote(userTraffic={"usedTrafficBytes": 123})
        adapter, transport = self.adapter([ok(remote)])
        result = await adapter.read_public_usage(
            {
                **self.snapshot(),
                "remote_user_id": REMOTE,
                "subscription_url": remote["subscriptionUrl"],
            }
        )
        self.assertEqual(result.used_bytes, 123)
        self.assertEqual([r[0] for r in transport.requests], ["GET"])
