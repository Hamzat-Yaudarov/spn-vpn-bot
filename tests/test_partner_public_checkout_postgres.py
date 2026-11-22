"""Renewal-only capability checkout on isolated real SQL/WASM and fake merchant.

No live settings, customer DB, merchant, Telegram, DNS or VPN writes. Native
multi-connection races remain an additional release gate, not simulated here.
"""

from dataclasses import replace
from datetime import timedelta
import os
import unittest
from uuid import uuid4

import test_partner_invoice_intent_postgres as fixtures
from services.partner_checkout import CheckoutError, PartnerCheckoutService
from services.partner_public_checkout import (
    InvalidPublicCheckout,
    PartnerPublicCheckoutService,
    PartnerPublicCheckoutStore,
    PublicCheckoutConflict,
    PublicCheckoutUnavailable,
)
from services.partner_subscriptions import (
    PartnerLinkSettings,
    PartnerSubscriptionLinks,
    PartnerSubscriptionService,
    SubscriptionNotFound,
)


NOW, ROOT = fixtures.NOW, fixtures.ROOT


@unittest.skipUnless(
    os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM not configured"
)
class PublicCheckoutPostgresTests(unittest.IsolatedAsyncioTestCase):
    restarted = fixtures.InvoiceIntentPostgresTests.restarted
    subscription = fixtures.InvoiceIntentPostgresTests.subscription
    pay = fixtures.InvoiceIntentPostgresTests.pay
    complete = fixtures.InvoiceIntentPostgresTests.complete

    async def asyncSetUp(self):
        await fixtures.InvoiceIntentPostgresTests.asyncSetUp(self)
        await self.sql.query(
            (ROOT / "ops/partner-platform/public_checkout.sql").read_text(), exec=True
        )
        self.sub = await self.subscription()
        self.links = PartnerSubscriptionLinks(
            PartnerLinkSettings(
                "partners.example.test", b"public-checkout-test-signing-32bytes"
            )
        )
        self.subscriptions = PartnerSubscriptionService(
            self.store, self.links, enabled=True, clock=lambda: self.now
        )
        context = await self.store.get_subscription_link_context(
            self.pid, 100, self.sub["id"]
        )
        self.slug = context["slug"]
        self.token = self.links.token(context)
        self.requests = PartnerPublicCheckoutStore(lambda: self.sql, enabled=True)
        self.service = self.restarted_service()

    def restarted_service(self, **options):
        return PartnerPublicCheckoutService(
            self.subscriptions,
            self.checkout,
            self.requests,
            enabled=True,
            clock=lambda: self.now,
            **options
        )

    async def purchase(self, *, key=None, price=19900):
        return await self.service.create_purchase(
            self.slug, self.token, key or str(uuid4()), price
        )

    async def test_explicit_flags_quote_and_expired_issued_subscription_renewable(self):
        disabled = PartnerPublicCheckoutService(
            self.subscriptions, self.checkout, self.requests
        )
        with self.assertRaises(PublicCheckoutUnavailable):
            await disabled.quote(self.slug, self.token)
        quote = await self.service.quote(self.slug, self.token)
        self.assertEqual(
            (quote["price_kopecks"], quote["days"], quote["renewal_available"]),
            (19900, 30, True),
        )
        self.now = NOW + timedelta(days=31)
        self.assertTrue(
            (await self.service.quote(self.slug, self.token))["renewal_available"]
        )
        result = await self.purchase()
        self.assertEqual(result["status"], "created")
        self.assertNotIn("tg_id", str(result))
        self.assertNotIn("subscription_url", str(result))

    async def test_paid_canonical_check_one_ledger_no_implicit_get_create(self):
        key = str(uuid4())
        result = await self.purchase(key=key)
        self.assertEqual(len(self.merchant.calls), 1)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1
        )
        self.assertEqual(
            (await self.service.get_purchase(self.slug, self.token, key))["status"],
            "created",
        )
        self.assertEqual(len(self.merchant.calls), 1)
        oid = result["id"]
        self.merchant.rows[oid] = replace(
            self.merchant.rows[oid], paid=True, status="succeeded"
        )
        paid = await self.service.get_purchase(self.slug, self.token, key)
        self.assertEqual(paid["status"], "paid_pending_activation")
        self.assertIn("Повторно платить не нужно", paid["message"])
        await self.service.get_purchase(self.slug, self.token, key)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 2
        )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT sum(partner_kopecks)::bigint FROM wl_revenue_ledger"
            ),
            27860,
        )
        with self.assertRaises(PublicCheckoutConflict):
            await self.purchase()

    async def test_changed_request_key_reuses_one_invoice_and_quote_restores_alias(
        self,
    ):
        first_key, second_key = str(uuid4()), str(uuid4())
        first = await self.purchase(key=first_key)
        second = await self.purchase(key=second_key)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["request_key"], second_key)
        self.assertEqual(len(self.merchant.calls), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 2)
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_public_renewal_requests"),
            2,
        )
        pending = (await self.service.quote(self.slug, self.token))["pending_purchase"]
        self.assertIn(pending["request_key"], {first_key, second_key})
        self.assertEqual(
            (
                await self.service.get_purchase(
                    self.slug, self.token, pending["request_key"]
                )
            )["id"],
            first["id"],
        )
        with self.assertRaises(PublicCheckoutConflict):
            await self.purchase(key=first_key, price=29900)

    async def test_lost_post_response_get_only_checks_then_same_post_resumes_frozen_invoice(
        self,
    ):
        key = str(uuid4())
        self.merchant.lost_response = True
        with self.assertRaises(CheckoutError):
            await self.purchase(key=key)
        self.assertEqual(len(self.merchant.calls), 1)
        recovered = await self.service.get_purchase(self.slug, self.token, key)
        self.assertEqual(recovered["status"], "created")
        self.assertNotIn("confirmation_url", recovered)
        self.assertEqual(len(self.merchant.calls), 1)
        await self.sql.execute(
            "UPDATE wl_projects SET price_kopecks=29900 WHERE id=$1", self.pid
        )
        self.service = self.restarted_service()
        result = await self.purchase(key=key)
        self.assertIn("confirmation_url", result)
        self.assertEqual(
            [row[0] for row in self.merchant.calls], [result["id"], result["id"]]
        )
        self.assertEqual(
            [row[1]["price_kopecks"] for row in self.merchant.calls], [19900, 19900]
        )

    async def test_price_changed_before_first_confirmation_no_order_or_invoice(self):
        await self.sql.execute(
            "UPDATE wl_projects SET price_kopecks=29900 WHERE id=$1", self.pid
        )
        with self.assertRaises(PublicCheckoutConflict) as caught:
            await self.purchase(price=19900)
        self.assertEqual(caught.exception.code, "price_changed")
        self.assertEqual(self.merchant.calls, [])
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_public_renewal_requests"),
            0,
        )
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 1)
        self.assertEqual((await self.purchase(price=29900))["price_kopecks"], 29900)

    async def test_existing_bot_renewal_is_recovered_only_after_explicit_post(self):
        bot = await self.store.create_order(
            self.pid, 100, str(uuid4()), 19900, subscription_id=self.sub["id"]
        )
        pending = (await self.service.quote(self.slug, self.token))["pending_purchase"]
        self.assertNotIn("request_key", pending)
        self.assertEqual(pending["id"], bot["id"])
        self.assertEqual(self.merchant.calls, [])
        key = str(uuid4())
        result = await self.purchase(key=key)
        self.assertEqual(result["id"], bot["id"])
        self.assertEqual(
            (await self.service.get_purchase(self.slug, self.token, key))["id"],
            bot["id"],
        )
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 2)

    async def test_first_unissued_slot_wrong_slug_or_rotated_capability_cannot_purchase(
        self,
    ):
        for slug, token in (("another-brand", self.token), (self.slug, "x" * 64)):
            with self.assertRaises(SubscriptionNotFound):
                await self.service.create_purchase(slug, token, str(uuid4()), 19900)
        context = await self.subscriptions._resolve(self.slug, self.token)
        await self.sql.execute(
            "UPDATE wl_subscriptions SET public_link_generation=1 WHERE id=$1",
            self.sub["id"],
        )
        with self.assertRaises(SubscriptionNotFound):
            await self.requests.prepare_purchase(context, str(uuid4()), 19900, now=NOW)
        with self.assertRaises(SubscriptionNotFound):
            await self.purchase()
        self.assertEqual(self.merchant.calls, [])

    async def test_other_subscription_cannot_read_purchase_by_request_key(self):
        key = str(uuid4())
        await self.purchase(key=key)
        other = await self.subscription(200)
        context = await self.store.get_subscription_link_context(
            self.pid, 200, other["id"]
        )
        with self.assertRaises(SubscriptionNotFound):
            await self.service.get_purchase(self.slug, self.links.token(context), key)

    async def test_manual_hold_new_renewal_blocked_without_quota_or_ledger_changes(
        self,
    ):
        await self.sql.execute(
            "UPDATE wl_subscriptions SET traffic_cycle_hold='legacy_review' WHERE id=$1",
            self.sub["id"],
        )
        quote = await self.service.quote(self.slug, self.token)
        self.assertFalse(quote["renewal_available"])
        self.assertEqual(quote["unavailable_reason"], "manual_review")
        with self.assertRaises(PublicCheckoutConflict):
            await self.purchase()
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1
        )
        self.assertEqual(
            await self.sql.fetchval(
                "SELECT traffic_limit_bytes FROM wl_subscriptions WHERE id=$1",
                self.sub["id"],
            ),
            300 * 1024**3,
        )

    async def test_pending_addon_cannot_be_misidentified_as_subscription_invoice(self):
        await self.store.create_addon_order(
            self.pid,
            100,
            str(uuid4()),
            2400,
            subscription_id=self.sub["id"],
            purpose="traffic",
            package_id="gb_10",
            now=NOW,
        )
        self.assertEqual(
            (await self.service.quote(self.slug, self.token))["unavailable_reason"],
            "purchase_in_progress",
        )
        with self.assertRaises(PublicCheckoutConflict):
            await self.purchase()
        self.assertEqual(self.merchant.calls, [])

    async def test_blocked_late_addon_does_not_permanently_block_paid_base_renewal(
        self,
    ):
        addon = await self.store.create_addon_order(
            self.pid,
            100,
            str(uuid4()),
            10000,
            subscription_id=self.sub["id"],
            purpose="devices",
            package_id="1",
            now=NOW,
        )
        await self.checkout.create_checkout(self.pid, 100, addon["id"])
        self.merchant.rows[addon["id"]] = replace(
            self.merchant.rows[addon["id"]], paid=True, status="succeeded"
        )
        self.now = NOW + timedelta(days=31)
        await self.checkout.reconcile(self.pid, 100, addon["id"])
        self.assertEqual(
            (await self.store.get_order(self.pid, 100, addon["id"]))[
                "activation_issue"
            ],
            "addon_expired",
        )
        quote = await self.service.quote(self.slug, self.token)
        self.assertTrue(quote["renewal_available"])
        self.assertNotIn("pending_purchase", quote)
        # Both existing late-add-on manual-review reasons leave the already
        # paid base renewable; the original payment remains in the ledger.
        await self.sql.execute(
            "UPDATE wl_orders SET activation_issue='device_limit' WHERE id=$1",
            addon["id"],
        )
        await self.sql.execute(
            "UPDATE wl_activation_jobs SET blocked_reason='device_limit' WHERE order_id=$1",
            addon["id"],
        )
        self.assertTrue(
            (await self.service.quote(self.slug, self.token))["renewal_available"]
        )
        renewed = await self.purchase()
        self.assertEqual(renewed["status"], "created")
        self.assertNotEqual(renewed["id"], addon["id"])
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 2
        )

    async def test_activation_expired_manual_hold_still_blocks_new_renewal(self):
        key = str(uuid4())
        pending = await self.purchase(key=key)
        self.merchant.rows[pending["id"]] = replace(
            self.merchant.rows[pending["id"]], paid=True, status="succeeded"
        )
        await self.service.get_purchase(self.slug, self.token, key)
        await self.sql.execute(
            "UPDATE wl_orders SET activation_issue='activation_expired' WHERE id=$1",
            pending["id"],
        )
        await self.sql.execute(
            "UPDATE wl_activation_jobs SET blocked_reason='activation_expired' WHERE order_id=$1",
            pending["id"],
        )
        quote = await self.service.quote(self.slug, self.token)
        self.assertFalse(quote["renewal_available"])
        self.assertEqual(quote["unavailable_reason"], "activation_pending")
        with self.assertRaises(PublicCheckoutConflict):
            await self.purchase()
        self.assertEqual(len(self.merchant.calls), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 2)

    async def test_alias_insert_failure_rolls_back_new_order(self):
        execute = self.sql.execute

        async def fail(sql, *args):
            if "INSERT INTO wl_public_renewal_requests" in sql:
                raise RuntimeError("injected alias failure")
            return await execute(sql, *args)

        self.sql.execute = fail
        with self.assertRaisesRegex(RuntimeError, "injected"):
            await self.purchase()
        self.sql.execute = execute
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 1)
        self.assertEqual(self.merchant.calls, [])

    async def test_migration_reapply_no_financial_change_and_input_types(self):
        await self.sql.query(
            (ROOT / "ops/partner-platform/public_checkout.sql").read_text(), exec=True
        )
        for price in (True, 19900.0, 1, -19900):
            with self.assertRaises(InvalidPublicCheckout):
                await self.purchase(price=price)
        with self.assertRaises(InvalidPublicCheckout):
            await self.purchase(key="not-uuid")
        self.assertEqual(
            await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1
        )
