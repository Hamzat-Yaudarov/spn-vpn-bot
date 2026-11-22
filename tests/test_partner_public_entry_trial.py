"""Explicit public enrollment trial; no real merchant, network or database."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock

from services.partner_checkout import CheckoutError
from services.partner_entry_pricing import EntryPricingError, EntryPricingPolicy
from services.partner_runtime import (
    PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL, PartnerRuntimeWorker,
    RuntimeConfigurationError, build_runtime,
)
from services.partner_runtime_catalog import StagingAddonCatalog
from services.partner_yookassa import PartnerYooKassaProvider, PartnerYooKassaSettings
from test_partner_owner_pilot_runtime import owner_pilot, pilot_payment
from test_partner_runtime import FakeConnection, FakePool
from test_partner_yookassa import Factory, Response, SHOP, SECRET, ORDER, PROJECT, PAYMENT


def public_trial(**changes):
    return owner_pilot(**{
        "enable_owner_entry_pilot": False, "enable_public_entry_trial": True,
        "manager_allowed_user_ids": None, **changes,
    })


class PublicEntryTrialPricingTests(unittest.TestCase):
    def test_all_positive_owners_get_ten_rubles_only_under_explicit_flag(self):
        policy = EntryPricingPolicy(public_trial_enabled=True)
        for owner in (1, 42, 6910097562, 2**63 - 1):
            self.assertEqual(policy.price_for(owner), 1000)
            self.assertFalse(policy.is_pilot_for(owner))
            row = {"owner_tg_id": owner, "price_kopecks": 1000}
            policy.validate_new_invoice(row)
            EntryPricingPolicy().validate_saved_order(row)
            with self.assertRaises(EntryPricingError):
                EntryPricingPolicy().validate_new_invoice(row)
            self.assertEqual(row["price_kopecks"], 1000)
        for owner in (True, 0, -1, None, "42", 2**63):
            with self.assertRaises(EntryPricingError): policy.price_for(owner)

    def test_trial_and_owner_flags_are_boolean_and_mutually_exclusive(self):
        for args in ({"public_trial_enabled": 1}, {"public_trial_enabled": "true"},
                     {"public_trial_enabled": None},
                     {"public_trial_enabled": True, "owner_pilot_enabled": True}):
            with self.assertRaises(EntryPricingError): EntryPricingPolicy(**args)

    def test_trial_does_not_silently_reprice_an_existing_normal_invoice(self):
        row = {"owner_tg_id": 42, "price_kopecks": 999000}
        EntryPricingPolicy(public_trial_enabled=True).validate_saved_order(row)
        with self.assertRaises(EntryPricingError):
            EntryPricingPolicy(public_trial_enabled=True).validate_new_invoice(row)
        self.assertEqual(row["price_kopecks"], 999000)


class PublicEntryTrialRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_access_never_enables_customer_billing_provisioning_or_other_recovery(self):
        settings = public_trial()
        self.assertTrue(settings.preflight()["configured"])
        self.assertFalse(settings.preflight()["production_ready"])
        provider = AsyncMock()
        runtime = build_runtime(settings, AsyncMock(), addon_loader=StagingAddonCatalog().quote, provider=provider)
        self.assertIs(runtime.entry.provider, provider)
        self.assertIsNone(runtime.checkout.provider)
        self.assertIsNone(runtime.checkout.provisioner)
        self.assertFalse(runtime.commerce.entitlements_enabled)
        self.assertEqual(runtime.enrollment.entry_pricing.price_for(42), 1000)
        runtime.probe = AsyncMock(return_value={"database_ready": True})
        runtime.enrollment.list_recoverable_entry_orders = AsyncMock(return_value=[])
        runtime.recovery.recover_once = AsyncMock()
        unrelated = AsyncMock()
        runtime.register_worker_hook("unrelated", unrelated, enabled=True)
        result = await PartnerRuntimeWorker(runtime).run_once()
        self.assertEqual(result["phase_errors"], 0)
        self.assertIn("entries", result)
        self.assertNotIn("commerce", result)
        self.assertNotIn("unrelated", result)
        runtime.recovery.recover_once.assert_not_awaited()
        unrelated.assert_not_awaited()
        provider.create_invoice.assert_not_awaited()

    async def test_composed_provider_is_strictly_live_public_entry_only(self):
        runtime = build_runtime(public_trial(), AsyncMock(), addon_loader=StagingAddonCatalog().quote)
        self.assertTrue(runtime.entry.provider.settings.public_entry_trial)
        self.assertFalse(runtime.entry.provider.settings.owner_entry_pilot)
        self.assertFalse(runtime.entry.provider.settings.test_mode)
        self.assertIsNone(runtime.checkout.provider)

    async def test_public_trial_fails_closed_if_guards_or_required_boundaries_change(self):
        for changes in (
            {"enable_owner_entry_pilot": True}, {"enable_public_entry_trial": 1},
            {"manager_allowed_user_ids": ()}, {"manager_allowed_user_ids": (6910097562,)},
            {"operator_ids": ()}, {"operator_ids": (42,)}, {"admin_recipient_id": 42},
            {"enable_billing": False}, {"yookassa_test_mode": True},
            {"enable_worker": False}, {"enable_launches": False},
            {"enable_customer_webhooks": False}, {"enable_manager_webhook": False},
            {"allow_staging_writes": False}, {"allow_staging_network": False},
            {"enable_provisioning": True}, {"enable_public_gateway": True},
            {"enable_public_checkout": True}, {"invoice_intents_enabled": True},
            {"enable_notifications": True}, {"enable_marketing_delivery": True},
            {"enable_support_delivery": True}, {"enable_finance_notifications": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(RuntimeConfigurationError):
                public_trial(**changes)

    async def test_exact_public_schema_proof_required(self):
        keys = {"price_constraint_safe", "owner_pilot_unique_safe", "financial_snapshot_guard_safe"}

        class Connection(FakeConnection):
            async def fetchrow(self, query):
                if query == PUBLIC_ENTRY_TRIAL_SCHEMA_CHECK_SQL: return self.guard
                return await super().fetchrow(query)

        for failed in (None, *keys):
            conn = Connection()
            conn.guard = {key: key != failed for key in keys}
            runtime = build_runtime(public_trial(), lambda: FakePool(conn), addon_loader=StagingAddonCatalog().quote)
            result = await runtime.probe()
            self.assertEqual(result["database_ready"], failed is None, result)
            if failed: self.assertIn("public_entry_trial_migration_required", result["blockers"])


class PublicEntryTrialProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, *, body=None, **changes):
        response = pilot_payment() if body is None else body
        if body is None: response["metadata"]["wl_tg_id"] = "42"
        factory = Factory(Response(response))
        settings = PartnerYooKassaSettings(SHOP, SECRET, "https://return.example.test/paid",
            **{"test_mode": False, "public_entry_trial": True, **changes})
        return PartnerYooKassaProvider(settings, session_factory=factory), factory

    @staticmethod
    def order(**changes):
        return {"id": ORDER, "project_id": PROJECT, "tg_id": 42,
                "price_kopecks": 1000, "purpose": "entry", **changes}

    async def test_public_owner_is_charged_exact_ten_rubles_with_stable_order_key(self):
        provider, factory = self.provider()
        first = await provider.create_invoice(self.order(), idempotence_key=ORDER)
        second = await provider.create_invoice(self.order(), idempotence_key=ORDER)
        self.assertEqual(first, second)
        self.assertEqual((first.tg_id, first.amount_kopecks, first.purpose), (42, 1000, "entry"))
        for _, _, request in factory.requests:
            self.assertEqual(request["headers"]["Idempotence-Key"], ORDER)
            self.assertEqual(request["json"]["amount"], {"value": "10.00", "currency": "RUB"})

    async def test_new_invoice_cannot_charge_old_price_customer_subscription_or_addon(self):
        provider, factory = self.provider()
        for changes in ({"price_kopecks": 999000}, {"price_kopecks": 999},
                        {"price_kopecks": 1000.0}, {"tg_id": True},
                        {"purpose": "subscription", "price_kopecks": 19900},
                        {"purpose": "devices"}, {"purpose": "traffic"}):
            with self.subTest(changes=changes), self.assertRaises(CheckoutError):
                await provider.create_invoice(self.order(**changes), idempotence_key=ORDER)
        self.assertEqual(factory.requests, [])

    async def test_canonical_receipts_keep_preexisting_fee_but_reject_wrong_money_or_purpose(self):
        for fee in ("10.00", "9990.00"):
            body = pilot_payment()
            body["metadata"]["wl_tg_id"] = "42"
            body["amount"]["value"] = fee
            provider, _ = self.provider(body=body)
            self.assertEqual((await provider.get_payment(PAYMENT)).amount_kopecks, int(float(fee) * 100))
        for key, value in (("test", True), ("amount", {"value": "9.99", "currency": "RUB"}),
                           ("amount", {"value": "10.00", "currency": "USD"}),
                           ("metadata", {"wl_kind": "subscription", "wl_tg_id": "42"})):
            body = deepcopy(pilot_payment())
            body[key] = value
            provider, _ = self.provider(body=body)
            with self.subTest(key=key), self.assertRaises(CheckoutError): await provider.get_payment(PAYMENT)

    async def test_trial_provider_flags_are_boolean_exclusive_and_live(self):
        for changes in ({"public_entry_trial": 1}, {"public_entry_trial": "true"},
                        {"owner_entry_pilot": True}, {"test_mode": True}):
            with self.assertRaises(ValueError): self.provider(**changes)


if __name__ == "__main__": unittest.main()
