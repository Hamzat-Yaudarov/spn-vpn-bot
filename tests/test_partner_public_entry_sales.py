"""Regular entry-only fee, without live network, money or database writes."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock

from services.partner_checkout import CheckoutError
from services.partner_entry_pricing import EntryPricingError, EntryPricingPolicy
from services.partner_manager import PartnerManagerHandler
from services.partner_runtime import PartnerRuntimeWorker, RuntimeConfigurationError, build_runtime
from services.partner_runtime_catalog import StagingAddonCatalog
from services.partner_yookassa import PartnerYooKassaProvider, PartnerYooKassaSettings
from test_partner_owner_pilot_runtime import owner_pilot, pilot_payment
from test_partner_runtime_manager import BOT_ID, ORIGIN, update
from test_partner_yookassa import Factory, Response, SHOP, SECRET, ORDER, PROJECT, PAYMENT


def public_sales(**changes):
    return owner_pilot(**{
        "enable_owner_entry_pilot": False, "enable_public_entry_sales": True,
        "manager_allowed_user_ids": None, **changes,
    })


class PublicEntrySalesPolicyTests(unittest.TestCase):
    def test_regular_price_is_9990_for_every_valid_owner(self):
        policy = EntryPricingPolicy(public_sales_enabled=True)
        for owner in (1, 42, 6910097562, 2**63 - 1):
            self.assertEqual(policy.price_for(owner), 999000)
            self.assertFalse(policy.is_pilot_for(owner))
            for fee in (1000, 999000):
                row = {"owner_tg_id": owner, "price_kopecks": fee}
                before = deepcopy(row)
                policy.validate_saved_order(row)
                if fee == 999000:
                    policy.validate_new_invoice(row)
                else:
                    with self.assertRaises(EntryPricingError): policy.validate_new_invoice(row)
                self.assertEqual(row, before)

    def test_all_price_capabilities_are_strict_and_mutually_exclusive(self):
        for changes in ({"public_sales_enabled": 1}, {"public_sales_enabled": "true"},
                        {"public_sales_enabled": None},
                        {"public_sales_enabled": True, "public_trial_enabled": True},
                        {"public_sales_enabled": True, "owner_pilot_enabled": True}):
            with self.assertRaises(EntryPricingError): EntryPricingPolicy(**changes)


class PublicEntrySalesRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_regular_enrollment_cannot_enable_tenant_commerce_even_with_injected_ports(self):
        settings = public_sales()
        self.assertTrue(settings.preflight()["configured"])
        provider, provisioner = AsyncMock(), AsyncMock()
        runtime = build_runtime(settings, AsyncMock(), addon_loader=StagingAddonCatalog().quote,
                                provider=provider, provisioner=provisioner)
        self.assertIs(runtime.entry.provider, provider)
        self.assertIsNone(runtime.checkout.provider)
        self.assertIsNone(runtime.checkout.provisioner)
        self.assertFalse(runtime.commerce.entitlements_enabled)
        self.assertEqual(runtime.enrollment.entry_pricing.price_for(42), 999000)
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

    async def test_regular_provider_has_explicit_entry_only_live_capability(self):
        runtime = build_runtime(public_sales(), AsyncMock(), addon_loader=StagingAddonCatalog().quote)
        settings = runtime.entry.provider.settings
        self.assertTrue(settings.public_entry_sales)
        self.assertFalse(settings.owner_entry_pilot)
        self.assertFalse(settings.public_entry_trial)
        self.assertFalse(settings.test_mode)
        self.assertIsNone(runtime.checkout.provider)

    async def test_public_regular_fee_keeps_required_boundaries_and_exclusive_flags(self):
        for changes in (
            {"enable_owner_entry_pilot": True}, {"enable_public_entry_trial": True},
            {"enable_public_entry_sales": 1}, {"manager_allowed_user_ids": ()},
            {"manager_allowed_user_ids": (6910097562,)}, {"operator_ids": ()},
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
                public_sales(**changes)

    async def test_payment_first_requires_regular_entry_and_managed_bots(self):
        for changes in ({"enable_managed_bots": False},
                        {"enable_public_entry_sales": False}):
            with self.assertRaises(RuntimeConfigurationError):
                public_sales(enable_payment_first_entry=True, **changes)
        runtime = build_runtime(public_sales(enable_payment_first_entry=True, enable_managed_bots=True),
                                AsyncMock(), addon_loader=StagingAddonCatalog().quote)
        self.assertTrue(runtime.enrollment.payment_first_enabled)
        self.assertTrue(runtime.managed_bots.store.payment_first_enabled)

    async def test_central_start_is_short_and_neutral_and_matches_regular_price(self):
        handler = PartnerManagerHandler(BOT_ID, ORIGIN,
            entry_pricing=EntryPricingPolicy(public_sales_enabled=True))
        response = await handler.handle(update())
        self.assertIn("9 990 ₽", response["text"])
        self.assertIn("под своим брендом", response["text"])
        self.assertLess(len(response["text"]), 400)
        for word in ("VPN", "ГБ", "антиглушилка", "обход", "10 ₽"):
            self.assertNotIn(word, response["text"])


class PublicEntrySalesProviderTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, *, body=None, **changes):
        response = pilot_payment() if body is None else body
        if body is None:
            response["metadata"]["wl_tg_id"] = "42"
            response["amount"]["value"] = "9990.00"
        factory = Factory(Response(response))
        settings = PartnerYooKassaSettings(SHOP, SECRET, "https://return.example.test/paid",
            **{"test_mode": False, "public_entry_sales": True, **changes})
        return PartnerYooKassaProvider(settings, session_factory=factory), factory

    @staticmethod
    def order(**changes):
        return {"id": ORDER, "project_id": PROJECT, "tg_id": 42,
                "price_kopecks": 999000, "purpose": "entry", **changes}

    async def test_new_invoice_is_regular_9990_and_keeps_same_idempotency_key(self):
        provider, factory = self.provider()
        result = await provider.create_invoice(self.order(), idempotence_key=ORDER)
        self.assertEqual((result.amount_kopecks, result.purpose), (999000, "entry"))
        request = factory.requests[0][2]
        self.assertEqual(request["headers"]["Idempotence-Key"], ORDER)
        self.assertEqual(request["json"]["amount"], {"value": "9990.00", "currency": "RUB"})
        self.assertEqual(request["json"]["description"], "Подключение партнёрского проекта")

    async def test_old_10_fee_and_customer_purchases_cannot_create_new_invoices(self):
        provider, factory = self.provider()
        for changes in ({"price_kopecks": 1000}, {"price_kopecks": 999000.0},
                        {"price_kopecks": True}, {"tg_id": True},
                        {"purpose": "subscription", "price_kopecks": 19900},
                        {"purpose": "devices"}, {"purpose": "traffic"}):
            with self.subTest(changes=changes), self.assertRaises(CheckoutError):
                await provider.create_invoice(self.order(**changes), idempotence_key=ORDER)
        self.assertEqual(factory.requests, [])

    async def test_historical_ten_and_regular_fee_receipts_remain_reconcilable(self):
        for fee, kopecks in (("10.00", 1000), ("9990.00", 999000)):
            body = pilot_payment()
            body["metadata"]["wl_tg_id"] = "42"
            body["amount"]["value"] = fee
            provider, _ = self.provider(body=body)
            self.assertEqual((await provider.get_payment(PAYMENT)).amount_kopecks, kopecks)
        for changes in ({"test": True}, {"amount": {"value": "9.99", "currency": "RUB"}},
                        {"amount": {"value": "10.00", "currency": "USD"}},
                        {"metadata": {"wl_kind": "subscription", "wl_tg_id": "42"}}):
            body = {**deepcopy(pilot_payment()), **changes}
            provider, _ = self.provider(body=body)
            with self.assertRaises(CheckoutError): await provider.get_payment(PAYMENT)

    async def test_sales_flags_are_explicit_exclusive_and_live(self):
        for changes in ({"public_entry_sales": 1}, {"public_entry_sales": "true"},
                        {"public_entry_trial": True}, {"owner_entry_pilot": True},
                        {"test_mode": True}):
            with self.assertRaises(ValueError): self.provider(**changes)


if __name__ == "__main__": unittest.main()
