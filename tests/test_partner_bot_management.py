"""Customer menu/consent and safe draft cancellation, never sent to Telegram."""

import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from services.partner_bot import PartnerBotHandler, _short_id
from services.partner_management import ManagementDenied
from test_partner_bot import FakeStore, project, message, callback, payload, buttons


class BotManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStore()
        self.management = AsyncMock()
        self.management.authorize.side_effect = ManagementDenied()
        self.management.get_marketing_consent.return_value = {
            "enabled": False,
            "blocked": False,
        }
        self.handler = PartnerBotHandler(
            self.store,
            management=self.management,
            manager_url="https://t.me/CentralManagerBot",
        )

    async def test_campaign_is_passed_only_from_valid_private_start(self):
        campaign = "m_" + "a" * 20
        await self.handler.handle(project(), message("/start " + campaign))
        self.management.register_customer.assert_awaited_once_with(
            project()["id"], 222, referrer_tg_id=None, campaign_code=campaign
        )
        self.store.calls.clear()
        self.management.register_customer.reset_mock()
        await self.handler.handle(project(), message("not /start " + campaign))
        self.management.register_customer.assert_awaited_once_with(
            project()["id"], 222, referrer_tg_id=None, campaign_code=None
        )

    async def test_plain_customer_navigation_skips_unneeded_staff_read_but_registers(self):
        for action in ("help", "connect", "connect:android", "connect:iphone", "buy", "subscriptions", "news"):
            with self.subTest(action=action):
                self.management.authorize.reset_mock()
                self.management.register_customer.reset_mock()
                await self.handler.handle(project(), callback(action))
                self.management.register_customer.assert_awaited_once()
                self.management.authorize.assert_not_awaited()

    async def test_menu_and_admin_always_read_current_staff_permission_without_cache(self):
        self.management.authorize.side_effect = None
        self.management.authorize.return_value = {"role": "support"}
        result = await self.handler.handle(project(), callback("menu"))
        self.assertTrue(any(b.get("callback_data") == "admin" for b in buttons(payload(result))))
        self.management.authorize.assert_awaited_once_with(project()["id"], 222, "referrals")
        self.management.authorize.side_effect = ManagementDenied()
        result = await self.handler.handle(project(), callback("admin"))
        self.assertIn("Действие недоступно", payload(result)["text"])
        result = await self.handler.handle(project(), callback("menu"))
        self.assertFalse(any(b.get("callback_data") == "admin" for b in buttons(payload(result))))
        self.assertEqual(self.management.authorize.await_count, 3)

    async def test_news_is_opt_in_and_factual_notifications_preserved(self):
        result = await self.handler.handle(project(), callback("news"))
        self.assertTrue(
            any(b.get("callback_data") == "news_on" for b in buttons(payload(result)))
        )
        self.management.set_marketing_consent.assert_not_awaited()
        await self.handler.handle(project(), callback("news_on"))
        self.management.set_marketing_consent.assert_awaited_with(
            project()["id"], 222, True
        )
        result = await self.handler.handle(project(), message("/news_off"))
        self.management.set_marketing_consent.assert_awaited_with(
            project()["id"], 222, False
        )
        self.assertIn("оплатах", payload(result)["text"])

    async def test_staff_admin_link_opens_central_bot_not_tenant_authenticated_page(
        self,
    ):
        self.management.authorize.side_effect = None
        self.management.authorize.return_value = {"role": "support"}
        result = await self.handler.handle(project(), callback("admin"))
        self.assertIn(
            "https://t.me/CentralManagerBot",
            [b.get("url") for b in buttons(payload(result))],
        )

    async def test_owner_admin_has_explicit_platform_support_not_tenant_contact(self):
        result = await self.handler.handle(
            project(support_url="https://t.me/TenantSupportContact"), callback("admin", user_id=777)
        )
        support = next(b for b in buttons(payload(result)) if b["text"] == "Поддержка платформы")
        self.assertEqual(support["url"], "https://t.me/vpnislam")
        self.assertNotEqual(support["url"], "https://t.me/TenantSupportContact")

    async def test_draft_cancel_requires_confirmation_and_scoped_store_lock(self):
        self.store.invoice_intents_enabled = True
        order_id = str(uuid4())
        order = {
            "id": order_id,
            "status": "created",
            "price_kopecks": 19900,
            "provider_payment_id": None,
            "invoice_requested_at": None,
        }
        self.store.orders[(project()["id"], 222, order_id)] = order
        self.store.cancel_uninvoiced_order = AsyncMock(
            return_value={**order, "status": "canceled"}
        )
        result = await self.handler.handle(
            project(), callback("cancel_draft:" + _short_id(order_id))
        )
        self.assertIn("Отменить черновик?", payload(result)["text"])
        self.store.cancel_uninvoiced_order.assert_not_awaited()
        await self.handler.handle(
            project(), callback("confirm_cancel:" + _short_id(order_id))
        )
        self.store.cancel_uninvoiced_order.assert_awaited_once_with(
            project()["id"], 222, order_id
        )

    async def test_started_invoice_never_cancelable_as_draft(self):
        self.store.invoice_intents_enabled = True
        order = {
            "id": str(uuid4()),
            "status": "created",
            "price_kopecks": 19900,
            "invoice_requested_at": True,
        }
        screen = self.handler._order_screen(order)
        self.assertFalse(
            any(
                b.get("callback_data", "").startswith("cancel_draft:")
                for b in buttons(screen)
            )
        )
