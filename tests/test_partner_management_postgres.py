"""Isolated actual PG/WASM management SQL; no secrets, external sends or live DB."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from uuid import uuid4

from test_partner_store_postgres import RegistryBridge
from services.partner_store import PartnerStore, InvalidProject
from services.partner_commerce_store import PartnerCommerceStore
from services.partner_management import (
    PartnerManagementStore,
    PartnerMarketingDeliveryService,
    PartnerSupportDeliveryService,
    ManagementDenied,
    ManagementConflict,
)
from services.partner_notifications import NotificationResult


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PG/WASM is not configured"
)
class ManagementPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        root = Path(__file__).resolve().parents[1]
        for name in ("commerce", "entitlements", "traffic", "management"):
            await self.sql.query(
                (root / f"ops/partner-platform/{name}.sql").read_text(), exec=True
            )
        self.registry = PartnerStore(lambda: self.sql)
        self.commerce = PartnerCommerceStore(lambda: self.sql)
        self.store = PartnerManagementStore(lambda: self.sql, delivery_enabled=True)
        self.p = await self.registry.create_project(
            42, "management-test-123", "manager-test", "Мой VPN"
        )
        self.pid = self.p["id"]
        await self.registry.save_bot_connection(
            self.pid, 42, 1, 123456, "ManageBot", "cipher:" + "x" * 100
        )
        await self.sql.execute(
            "UPDATE wl_projects SET status='active' WHERE id=$1", self.pid
        )
        self.now = datetime.now(timezone.utc) + timedelta(seconds=2)

    async def member(self, tg, role):
        await self.store.record_manager_identity(tg)
        return await self.store.set_member(
            self.pid, 42, str(uuid4()), tg_id=tg, role=role, expected_version=0
        )

    async def paid(self, tg=100):
        await self.store.register_customer(self.pid, tg)
        order = await self.commerce.create_order(self.pid, tg, str(uuid4()), 19900)
        await self.commerce.save_invoice(
            self.pid, tg, order["id"], "pay-" + order["id"], ""
        )
        await self.commerce.record_verified_payment(
            self.pid, order["id"], "pay-" + order["id"], 19900, "RUB", now=self.now
        )
        await self.commerce.complete_activation(
            self.pid,
            order["id"],
            remote_user_id="remote-" + str(tg),
            subscription_url="https://private.example.org/secret-credential",
        )
        return order

    async def broadcast(self, consent=(100, 101)):
        for tg in (100, 101, 102):
            await self.store.register_customer(self.pid, tg)
            if tg in consent:
                await self.store.set_marketing_consent(self.pid, tg, True)
        draft = await self.store.save_broadcast(
            self.pid, 42, str(uuid4()), text="Новости проекта"
        )
        review = await self.store.review_broadcast(
            self.pid, 42, draft["id"], draft["version"]
        )
        queued = await self.store.confirm_broadcast(
            self.pid,
            42,
            str(uuid4()),
            broadcast_id=draft["id"],
            expected_version=review["version"],
            review_token=review["review_token"],
        )
        return draft, review, queued

    async def test_schema_repeat_isolation_and_roles(self):
        schema = (
            Path(__file__).resolve().parents[1] / "ops/partner-platform/management.sql"
        ).read_text()
        await self.sql.query(schema, exec=True)
        await self.member(51, "viewer")
        await self.member(52, "support")
        await self.member(53, "marketer")
        self.assertEqual(
            (await self.store.authorize(self.pid, 51, "analytics"))["role"], "viewer"
        )
        for who, permission in (
            (51, "marketing"),
            (52, "analytics"),
            (53, "customers"),
            (53, "team"),
            (99, "analytics"),
        ):
            with self.assertRaises(ManagementDenied):
                await self.store.authorize(self.pid, who, permission)
        foreign = await self.registry.create_project(
            99, "foreign-test-123", "foreign-vpn", "Чужой VPN"
        )
        with self.assertRaises(ManagementDenied):
            await self.store.customers(foreign["id"], 51)
        visible = await self.store.visible_projects(51)
        self.assertEqual(len(visible["items"]), 1)
        self.assertEqual(visible["items"][0]["bot"]["username"], "ManageBot")
        self.assertNotIn("cipher", str(visible))

    async def test_team_trusted_id_version_owner_and_replay(self):
        key = str(uuid4())
        with self.assertRaises(InvalidProject):
            await self.store.set_member(
                self.pid, 42, key, tg_id=55, role="support", expected_version=0
            )
        await self.store.record_manager_identity(55)
        first = await self.store.set_member(
            self.pid, 42, key, tg_id=55, role="support", expected_version=0
        )
        self.assertEqual(
            first,
            await self.store.set_member(
                self.pid, 42, key, tg_id=55, role="support", expected_version=0
            ),
        )
        with self.assertRaises(ManagementConflict):
            await self.store.set_member(
                self.pid, 42, key, tg_id=55, role="viewer", expected_version=0
            )
        with self.assertRaises(InvalidProject):
            await self.store.set_member(
                self.pid, 42, str(uuid4()), tg_id=42, role=None, expected_version=0
            )
        with self.assertRaises(ManagementConflict):
            await self.store.set_member(
                self.pid, 42, str(uuid4()), tg_id=55, role="viewer", expected_version=0
            )
        removed = await self.store.set_member(
            self.pid, 42, str(uuid4()), tg_id=55, role=None, expected_version=1
        )
        self.assertFalse(removed["active"])
        with self.assertRaises(ManagementDenied):
            await self.store.authorize(self.pid, 55, "customers")

    async def test_allowlisted_paginated_data_no_private_links(self):
        await self.paid(100)
        await self.store.register_customer(self.pid, 101)
        await self.store.register_customer(self.pid, 102)
        result = await self.store.customers(self.pid, 42, limit=2)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["next_cursor"], 2)
        self.assertEqual(
            len((await self.store.customers(self.pid, 42, limit=2, cursor=2))["items"]),
            1,
        )
        self.assertEqual(
            len((await self.store.customers(self.pid, 42, query="100"))["items"]), 1
        )
        for method in (self.store.subscriptions, self.store.orders):
            payload = await method(self.pid, 42)
            for field in (
                "subscription_url",
                "remote_user_id",
                "encrypted_token",
                "confirmation_url",
                "provider_payment_id",
            ):
                self.assertNotIn(field, str(payload))
        self.assertEqual(
            (await self.store.overview(self.pid, 42))["partner_kopecks"], 13930
        )
        for kwargs in (
            {"limit": 0},
            {"cursor": 10001},
            {"limit": True},
            {"query": "%"},
        ):
            with self.assertRaises(InvalidProject):
                await self.store.customers(self.pid, 42, **kwargs)

    async def test_campaign_atomic_first_start_existing_no_reassignment(self):
        key = str(uuid4())
        campaign = await self.store.save_campaign(
            self.pid, 42, key, title="Личная реклама"
        )
        replay = await self.store.save_campaign(
            self.pid, 42, key, title="Личная реклама"
        )
        self.assertEqual(campaign, replay)
        self.assertIn("https://t.me/ManageBot?start=m_", campaign["link"])
        await self.store.register_customer(
            self.pid, 100, campaign_code=campaign["start_payload"]
        )
        other = await self.store.save_campaign(
            self.pid, 42, str(uuid4()), title="Другая реклама"
        )
        await self.store.register_customer(
            self.pid, 100, campaign_code=other["start_payload"]
        )
        await self.store.register_customer(self.pid, 101)
        await self.store.register_customer(
            self.pid, 101, campaign_code=campaign["start_payload"]
        )
        # Legacy customer registered by old commerce route is not retro-attributed.
        await self.commerce.register_customer(self.pid, 102)
        await self.store.register_customer(
            self.pid, 102, campaign_code=campaign["start_payload"]
        )
        stats = await self.store.campaigns(self.pid, 42)
        indexed = {r["id"]: r for r in stats["items"]}
        self.assertEqual(indexed[campaign["id"]]["registrations"], 1)
        self.assertEqual(indexed[other["id"]]["registrations"], 0)
        self.assertIsNone(
            (await self.commerce.get_customer(self.pid, 100))["referrer_tg_id"]
        )
        await self.paid(100)
        stats = await self.store.campaigns(self.pid, 42)
        actual = next(r for r in stats["items"] if r["id"] == campaign["id"])
        self.assertEqual(
            (
                actual["first_purchases"],
                actual["repeat_purchases"],
                actual["revenue_kopecks"],
            ),
            (1, 0, 19900),
        )

    async def test_repeat_registration_is_one_read_without_rebinding_or_transaction(self):
        await self.store.register_customer(self.pid, 100)
        original = await self.store.register_customer(self.pid, 101, referrer_tg_id=100)
        queries = []
        query = self.sql.query

        async def counted(sql, *params, **kwargs):
            queries.append(sql)
            return await query(sql, *params, **kwargs)

        self.sql.query = counted
        try:
            # A new/invalid referral or campaign cannot rebind an old customer.
            replay = await self.store.register_customer(
                self.pid, 101, referrer_tg_id=999999, campaign_code="m_" + "a" * 20
            )
        finally:
            self.sql.query = query
        self.assertEqual(replay, original)
        self.assertEqual(replay["referrer_tg_id"], 100)
        self.assertEqual(len(queries), 1, queries)
        self.assertTrue(queries[0].lstrip().startswith("SELECT c.*"))
        self.assertNotIn("FOR UPDATE", queries[0])

    async def test_legacy_customer_freezes_empty_attribution_before_fast_repeat(self):
        await self.commerce.register_customer(self.pid, 101)
        campaign = await self.store.save_campaign(self.pid, 42, str(uuid4()), title="Не задним числом")
        await self.store.register_customer(self.pid, 101, campaign_code=campaign["start_payload"])
        attribution = await self.sql.fetchrow(
            "SELECT campaign_id FROM wl_campaign_attributions WHERE project_id=$1 AND tg_id=101", self.pid
        )
        self.assertIsNotNone(attribution)
        self.assertIsNone(attribution["campaign_id"])
        replay = await self.store.register_customer(self.pid, 101, campaign_code=campaign["start_payload"])
        self.assertEqual(replay["tg_id"], 101)
        self.assertIsNone(await self.sql.fetchval(
            "SELECT campaign_id FROM wl_campaign_attributions WHERE project_id=$1 AND tg_id=101", self.pid
        ))

    async def test_pure_authorization_is_one_fresh_read_and_role_revocation_is_immediate(self):
        await self.member(51, "viewer")
        queries = []
        query = self.sql.query

        async def counted(sql, *params, **kwargs):
            queries.append(sql)
            return await query(sql, *params, **kwargs)

        self.sql.query = counted
        try:
            result = await self.store.authorize(self.pid, 51, "analytics")
        finally:
            self.sql.query = query
        self.assertEqual(result["role"], "viewer")
        self.assertEqual(len(queries), 1, queries)
        self.assertNotIn("FOR UPDATE", queries[0])
        await self.sql.execute(
            "UPDATE wl_project_members SET active=false WHERE project_id=$1 AND tg_id=51", self.pid
        )
        with self.assertRaises(ManagementDenied):
            await self.store.authorize(self.pid, 51, "analytics")

    async def test_campaign_archive_foreign_and_referrer_immutable(self):
        await self.store.register_customer(self.pid, 100)
        campaign = await self.store.save_campaign(
            self.pid, 42, str(uuid4()), title="Истёкшая"
        )
        await self.store.save_campaign(
            self.pid,
            42,
            str(uuid4()),
            title="Истёкшая",
            campaign_id=campaign["id"],
            expected_version=1,
            status="archived",
        )
        await self.store.register_customer(
            self.pid, 101, 100, campaign_code=campaign["code"]
        )
        self.assertIsNone(
            await self.sql.fetchval(
                "SELECT campaign_id FROM wl_campaign_attributions WHERE project_id=$1 AND tg_id=101",
                self.pid,
            )
        )
        self.assertEqual(
            (await self.commerce.get_customer(self.pid, 101))["referrer_tg_id"], 100
        )
        self.assertEqual((await self.store.referrals(self.pid, 42))["invited"], 1)
        with self.assertRaises(ManagementConflict):
            await self.store.save_campaign(
                self.pid,
                42,
                str(uuid4()),
                title="Подмена",
                campaign_id=str(uuid4()),
                expected_version=1,
            )

    async def test_broadcast_review_snapshot_optin_only_and_replay(self):
        for tg in (100, 101, 102):
            await self.store.register_customer(self.pid, tg)
        self.assertEqual(
            await self.store.get_marketing_consent(self.pid, 100),
            {"enabled": False, "blocked": False},
        )
        await self.store.set_marketing_consent(self.pid, 100, True)
        draft = await self.store.save_broadcast(
            self.pid, 42, str(uuid4()), text="Важная новость"
        )
        review = await self.store.review_broadcast(self.pid, 42, draft["id"], 1)
        self.assertEqual(review["eligible_recipients"], 1)
        # Later opt-in is NOT added silently; a reviewed snapshot is immutable.
        await self.store.set_marketing_consent(self.pid, 101, True)
        key = str(uuid4())
        queued = await self.store.confirm_broadcast(
            self.pid,
            42,
            key,
            broadcast_id=draft["id"],
            expected_version=2,
            review_token=review["review_token"],
        )
        self.assertEqual(queued["recipients"], {"queued": 1})
        self.assertEqual(
            queued,
            await self.store.confirm_broadcast(
                self.pid,
                42,
                key,
                broadcast_id=draft["id"],
                expected_version=2,
                review_token=review["review_token"],
            ),
        )
        self.assertTrue(queued["delivery_enabled"])
        with self.assertRaises(ManagementConflict):
            await self.store.save_broadcast(
                self.pid,
                42,
                str(uuid4()),
                text="Подменённый текст",
                broadcast_id=draft["id"],
                expected_version=3,
            )
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_customer_outbox"), 0
        )

    async def test_review_token_version_expiry_and_optout_before_confirmation(self):
        for tg in (100, 101):
            await self.store.register_customer(self.pid, tg)
            await self.store.set_marketing_consent(self.pid, tg, True)
        draft = await self.store.save_broadcast(
            self.pid, 42, str(uuid4()), text="Новость"
        )
        review = await self.store.review_broadcast(self.pid, 42, draft["id"], 1)
        with self.assertRaises(ManagementConflict):
            await self.store.confirm_broadcast(
                self.pid,
                42,
                str(uuid4()),
                broadcast_id=draft["id"],
                expected_version=2,
                review_token="x" * 43,
            )
        await self.store.set_marketing_consent(self.pid, 100, False)
        queued = await self.store.confirm_broadcast(
            self.pid,
            42,
            str(uuid4()),
            broadcast_id=draft["id"],
            expected_version=2,
            review_token=review["review_token"],
        )
        self.assertEqual(queued["recipients"], {"canceled": 1, "queued": 1})
        canceled = await self.store.cancel_broadcast(
            self.pid, 42, str(uuid4()), broadcast_id=draft["id"], expected_version=3
        )
        self.assertEqual(canceled["recipients"], {"canceled": 2})
        self.assertIsNone(await self.store.claim_marketing(now=self.now))
        fresh = await self.store.save_broadcast(
            self.pid, 42, str(uuid4()), text="Другая новость"
        )
        view = await self.store.review_broadcast(self.pid, 42, fresh["id"], 1)
        await self.sql.execute(
            "UPDATE wl_marketing_broadcasts SET reviewed_at=now()-interval '31 minutes' WHERE id=$1",
            fresh["id"],
        )
        with self.assertRaises(ManagementConflict):
            await self.store.confirm_broadcast(
                self.pid,
                42,
                str(uuid4()),
                broadcast_id=fresh["id"],
                expected_version=2,
                review_token=view["review_token"],
            )

    async def test_optout_cancels_lease_and_unknown_delivery_never_auto_resends(self):
        await self.broadcast()
        row = await self.store.claim_marketing(now=self.now)
        await self.store.set_marketing_consent(self.pid, row["tg_id"], False)
        self.assertFalse(await self.store.begin_marketing_send(row, now=self.now))
        row = await self.store.claim_marketing(now=self.now)
        self.assertTrue(await self.store.begin_marketing_send(row, now=self.now))
        self.assertTrue(
            await self.store.finish_marketing(
                row, NotificationResult("uncertain"), now=self.now
            )
        )
        self.assertIsNone(
            await self.store.claim_marketing(now=self.now + timedelta(hours=1))
        )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_marketing_recipients WHERE status='uncertain'"
            ),
            1,
        )

    async def test_lease_crashes_and_verified_late_outcome(self):
        await self.broadcast(consent=(100,))
        first = await self.store.claim_marketing(now=self.now)
        second = await self.store.claim_marketing(now=self.now + timedelta(minutes=3))
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertFalse(
            await self.store.begin_marketing_send(
                first, now=self.now + timedelta(minutes=3)
            )
        )
        self.assertTrue(
            await self.store.begin_marketing_send(
                second, now=self.now + timedelta(minutes=3)
            )
        )
        self.assertIsNone(
            await self.store.claim_marketing(now=self.now + timedelta(minutes=6))
        )
        self.assertTrue(
            await self.store.finish_marketing(
                second,
                NotificationResult("sent", 123),
                now=self.now + timedelta(minutes=6),
            )
        )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT telegram_message_id FROM wl_marketing_recipients WHERE tg_id=100"
            ),
            123,
        )

    async def test_disabled_sender_and_injected_worker_use_project_bot(self):
        await self.broadcast(consent=(100,))

        class Vault:
            def decrypt(self, project, bot, cipher):
                self.identity = (project, bot)
                return "test-token"

        class Sender:
            def __init__(self):
                self.sent = []

            async def verify(self, token, bot):
                self.bot = bot

            async def send(self, token, **kwargs):
                self.sent.append(kwargs)
                return NotificationResult("sent", 987)

        sender, vault = Sender(), Vault()
        disabled = PartnerMarketingDeliveryService(self.store, sender, vault=vault)
        self.assertFalse((await disabled.recover_once())["enabled"])
        self.assertEqual(sender.sent, [])
        worker = PartnerMarketingDeliveryService(
            self.store, sender, vault=vault, enabled=True, clock=lambda: self.now
        )
        result = await worker.recover_once()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(sender.sent[0]["bot_id"], 123456)
        self.assertEqual(sender.sent[0]["chat_id"], 100)
        self.assertIn("/news_off", sender.sent[0]["text"])
        self.assertEqual((await worker.recover_once())["sent"], 0)

    async def test_support_resend_paid_only_no_free_grants_or_link_leak(self):
        await self.member(55, "support")
        await self.member(56, "viewer")
        await self.store.register_customer(self.pid, 100)
        with self.assertRaises(ManagementConflict):
            await self.store.resend_subscription(self.pid, 55, str(uuid4()), tg_id=100)
        await self.paid(100)
        key = str(uuid4())
        first = await self.store.resend_subscription(self.pid, 55, key, tg_id=100)
        self.assertEqual(
            first, await self.store.resend_subscription(self.pid, 55, key, tg_id=100)
        )
        self.assertEqual(first["status"], "queued")
        self.assertNotIn("secret", str(first))
        with self.assertRaises(ManagementDenied):
            await self.store.resend_subscription(self.pid, 56, str(uuid4()), tg_id=100)
        with self.assertRaises(ManagementConflict):
            await self.store.resend_subscription(self.pid, 55, str(uuid4()), tg_id=100)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 1)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_activation_jobs"), 1
        )

    async def test_period_quota_not_cumulative_counter_or_fake_remaining(self):
        order = await self.paid(100)
        await self.sql.execute(
            """UPDATE wl_subscriptions SET traffic_limit_bytes=$2,
            traffic_cycle_baseline_bytes=$3,traffic_cycle_started_at=now(),traffic_cycle_hold=NULL WHERE id=$1""",
            order["subscription_id"],
            700 * 1024**3,
            350 * 1024**3,
        )
        sub = (await self.store.subscriptions(self.pid, 42))["items"][0]
        self.assertEqual(sub["traffic_period_limit_bytes"], 350 * 1024**3)
        self.assertEqual((sub["traffic_state"], sub["status"]), ("ready", "active"))
        self.assertNotIn("traffic_limit_bytes", sub)
        self.assertNotIn("remaining", str(sub))
        await self.sql.execute(
            "UPDATE wl_subscriptions SET traffic_cycle_hold='counter_changed' WHERE id=$1",
            order["subscription_id"],
        )
        sub = (await self.store.subscriptions(self.pid, 42))["items"][0]
        self.assertIsNone(sub["traffic_period_limit_bytes"])
        self.assertEqual(sub["traffic_state"], "review")

    async def test_support_worker_branded_link_only_to_customer_once(self):
        order = await self.paid(100)
        intent = await self.store.resend_subscription(
            self.pid, 42, str(uuid4()), tg_id=100
        )
        self.store.support_delivery_enabled = True

        class Vault:
            def decrypt(self, project, bot, cipher):
                return "test-token"

        class Pages:
            def __init__(self):
                self.calls = []

            async def link_for_customer(self, pid, tg, sub):
                self.calls.append((pid, tg, sub))
                return "https://brand.example.org/s/private-capability"

        class Sender:
            def __init__(self):
                self.sent = []

            async def verify(self, token, bot):
                pass

            async def send(self, token, **kwargs):
                self.sent.append(kwargs)
                return NotificationResult("sent", 101)

        sender, pages = Sender(), Pages()
        worker = PartnerSupportDeliveryService(
            self.store,
            sender,
            vault=Vault(),
            subscription_pages=pages,
            enabled=True,
            clock=lambda: self.now,
        )
        self.assertEqual((await worker.recover_once())["sent"], 1)
        self.assertEqual(pages.calls, [(self.pid, 100, order["subscription_id"])])
        self.assertEqual(sender.sent[0]["chat_id"], 100)
        self.assertIn("brand.example.org/s/private-capability", sender.sent[0]["text"])
        state = await self.store.get_resend(self.pid, 42, intent["id"])
        self.assertEqual(state["status"], "delivered")
        self.assertNotIn("private-capability", str(state))
        self.assertEqual((await worker.recover_once())["sent"], 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 1)

    async def test_support_unknown_post_and_expired_target_never_grants(self):
        order = await self.paid(100)
        await self.store.resend_subscription(self.pid, 42, str(uuid4()), tg_id=100)
        self.store.support_delivery_enabled = True
        row = await self.store.claim_support(now=self.now)
        self.assertTrue(await self.store.begin_support_send(row, now=self.now))
        self.assertIsNone(
            await self.store.claim_support(now=self.now + timedelta(minutes=3))
        )
        self.assertEqual(
            await self.sql.fetchval("SELECT status FROM wl_support_resend_intents"),
            "uncertain",
        )
        # A definite late result from the same attempt may resolve uncertainty.
        self.assertTrue(
            await self.store.finish_support(
                row, NotificationResult("sent", 99), now=self.now + timedelta(minutes=3)
            )
        )
        await self.sql.execute(
            "UPDATE wl_support_resend_intents SET created_at=now()-interval '11 minutes'"
        )
        await self.store.resend_subscription(self.pid, 42, str(uuid4()), tg_id=100)
        await self.sql.execute(
            "UPDATE wl_subscriptions SET expires_at=now()-interval '1 second' WHERE id=$1",
            order["subscription_id"],
        )
        self.assertIsNone(await self.store.claim_support(now=self.now))
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT count(*) FROM wl_support_resend_intents WHERE status='blocked'"
            ),
            1,
        )
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_activation_jobs"), 1
        )
