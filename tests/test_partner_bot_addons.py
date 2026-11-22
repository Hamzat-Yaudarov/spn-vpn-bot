"""Offline customer add-on/bonus screens and durable confirmation callbacks."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from test_partner_bot import (
    FakeCheckout, FakeStore, PROJECT_ID, SUBSCRIPTION_ID, buttons, callback, payload, project,
)
from services.partner_bot import PartnerBotHandler


class BotAddonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.store.entitlements_enabled = True
        self.expiry = datetime.now(timezone.utc) + timedelta(days=30)
        self.store.subscriptions[(PROJECT_ID, 222)] = [{"id": SUBSCRIPTION_ID, "project_id": PROJECT_ID,
            "tg_id": 222, "expires_at": self.expiry, "desired_expires_at": self.expiry,
            "device_limit": 3, "traffic_limit_bytes": 300*1024**3}]
        self.checkout = FakeCheckout(self.store)
        self.checkout.quote_addons = AsyncMock(return_value={"traffic_status": "available",
            "traffic_packages": [{"id": "gb_10", "gb": 10, "price_kopecks": 2400}],
            "devices": {"addon_status": "available", "packages": [{"count": 1, "price_kopecks": 1000}]}})
        self.orders = {}

        async def prepare(pid, tid, key, price, **kwargs):
            if key not in self.orders:
                order = {"id": str(uuid4()), "subscription_id": SUBSCRIPTION_ID, "project_id": pid,
                    "tg_id": tid, "price_kopecks": price, "status": "created", "purpose": kwargs["purpose"],
                    "package_id": kwargs["package_id"], "addon_quantity": 10 if kwargs["purpose"] == "traffic" else 1,
                    "addon_expires_at": None if kwargs["purpose"] == "traffic" else self.expiry}
                self.orders[key] = order
                self.store.orders[(pid, tid, order["id"])] = order
            return dict(self.orders[key])

        self.checkout.prepare_addon_order = AsyncMock(side_effect=prepare)
        self.handler = PartnerBotHandler(self.store, self.checkout)

    async def test_selection_saves_same_draft_repeat_and_requires_confirmation_before_invoice(self):
        for purpose, label in (("traffic", "24 ₽"), ("devices", "10 ₽")):
            listing = payload(await self.handler.handle(project(), callback(purpose)))
            choice = next(b["callback_data"] for b in buttons(listing) if b.get("callback_data", "").startswith("addon:"))
            self.assertLessEqual(len(choice.encode()), 64)
            self.assertIn(label, str(buttons(listing)))
            review = payload(await self.handler.handle(project(), callback(choice)))
            repeat = payload(await self.handler.handle(project(), callback(choice)))
            self.assertEqual(review, repeat)
            self.assertIn("Счёт ещё не создан", review["text"])
            self.assertEqual(self.checkout.calls, [])
            self.assertEqual(self.checkout.prepare_addon_order.await_args_list[-1].args,
                             self.checkout.prepare_addon_order.await_args_list[-2].args)
            confirm = next(b["callback_data"] for b in buttons(review) if b.get("callback_data", "").startswith("resume:"))
            paid_screen = payload(await self.handler.handle(project(), callback(confirm)))
            self.assertIn(label, paid_screen["text"])
            self.assertEqual(self.checkout.calls[-1][0], "create")
            self.checkout.calls.clear()

    async def test_disabled_expired_and_maximum_not_fake_available(self):
        self.store.entitlements_enabled = False
        screen = payload(await self.handler.handle(project(), callback("traffic")))
        self.assertIn("не подключена", screen["text"])
        self.checkout.quote_addons.assert_not_awaited()
        self.store.entitlements_enabled = True
        self.store.subscriptions[(PROJECT_ID, 222)][0]["expires_at"] = datetime.now(timezone.utc)-timedelta(days=1)
        screen = payload(await self.handler.handle(project(), callback("devices")))
        self.assertIn("продлите", screen["text"])
        self.checkout.quote_addons.assert_not_awaited()
        self.store.subscriptions[(PROJECT_ID, 222)][0]["expires_at"] = self.expiry
        self.checkout.quote_addons.return_value = {"devices": {"addon_status": "max_limit", "packages": []}}
        screen = payload(await self.handler.handle(project(), callback("devices")))
        self.assertIn("10 устройств", screen["text"])

    async def test_long_package_ids_and_invalid_callbacks_not_replaced_with_indexes(self):
        self.checkout.quote_addons.return_value["traffic_packages"][0]["id"] = "x"*17
        screen = payload(await self.handler.handle(project(), callback("traffic")))
        self.assertFalse(any(b.get("callback_data", "").startswith("addon:") for b in buttons(screen)))
        for value in ("addon:t:bad:gb_10:1", "addon:x:a:gb_10:2400", "addon:t:a:gb_10:-1"):
            await self.handler.handle(project(), callback(value))
        self.checkout.prepare_addon_order.assert_not_awaited()

    async def test_bonuses_short_invite_counts_only_real_loader_and_no_private_friends(self):
        self.store.get_referral_summary = AsyncMock(return_value={"invited_count": 3, "qualified_count": 2,
            "earned_days": 14, "pending_days": 7, "applied_days": 7, "referral_tg_id": 222})
        screen = payload(await self.handler.handle(project(), callback("bonuses")))
        self.assertIn("Лучший VPN: https://t.me/partner_test_bot?start=ref_222", screen["text"])
        self.assertIn("Пришло: 3 · Купили: 2", screen["text"])
        self.assertIn("Трафик и устройства бонусом не добавляются", screen["text"])
        self.assertEqual(buttons(screen)[0]["text"], "Пригласить друга")
        self.assertIn("ref_222", buttons(screen)[0]["url"])
        self.assertEqual(self.checkout.calls, [])

    async def test_addon_completed_and_manual_review_are_not_subscription_success(self):
        screen = self.handler._order_screen({"id": str(uuid4()), "purpose": "traffic", "status": "active"})
        self.assertIn("Докупка применена", screen["text"])
        screen = self.handler._order_screen({"id": str(uuid4()), "purpose": "devices", "status": "paid_pending_activation", "activation_issue": "addon_expired"})
        self.assertIn("поддержкой", screen["text"])
        self.assertFalse(any(b["text"] == "Оплатить" for b in buttons(screen)))

    async def test_paid_addon_pending_not_presented_as_applied_limits_and_can_resume(self):
        order_id = str(uuid4())
        self.store.orders[(PROJECT_ID, 222, order_id)] = {"id": order_id, "purpose": "traffic",
            "status": "paid_pending_activation", "price_kopecks": 2400}
        self.store.subscriptions[(PROJECT_ID, 222)][0]["traffic_limit_bytes"] = 310*1024**3
        screen = payload(await self.handler.handle(project(), callback("subscriptions")))
        self.assertIn("Новые лимиты: 310 ГБ", screen["text"])
        self.assertIn("ещё применяются", screen["text"])
        resume = next(b["callback_data"] for b in buttons(screen) if b["text"] == "Незавершённая покупка")
        review = payload(await self.handler.handle(project(), callback(resume)))
        self.assertIn("активация повторяется", review["text"])
        self.assertEqual(self.checkout.calls, [])

    async def test_cumulative_remote_cap_not_mislabeled_current_monthly_quota(self):
        sub=self.store.subscriptions[(PROJECT_ID,222)][0]
        sub.update(traffic_cycle_baseline_bytes=305*1024**3,traffic_limit_bytes=610*1024**3)
        screen=payload(await self.handler.handle(project(),callback("subscriptions")))
        self.assertIn("Квота периода: 305 ГБ",screen["text"])
        self.assertNotIn("610 ГБ",screen["text"])
