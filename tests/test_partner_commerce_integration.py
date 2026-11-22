"""HTTP/bot/checkout/actual SQL pipeline on an isolated in-memory PG engine.

External ports are deterministic fakes. No paid merchant, live Telegram,
Remnawave, production database or .env is touched. Native PG multi-connection
locking and production rollout remain separate release checks.
"""
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from fastapi import FastAPI

from partner_client_web import create_client_router, webhook_secret
from partner_web import create_router
from services.partner_bot import PartnerBotHandler
from services.partner_checkout import PartnerCheckoutService, PartnerPayment, ProvisionedSubscription, recover_partner_orders_once
from services.partner_commerce_store import PartnerCommerceStore
from services.partner_security import PartnerSettings
from services.partner_store import PartnerStore
from test_partner_store_postgres import RegistryBridge
from test_partner_web import MANAGER, authorization


class Payments:
    def __init__(self):
        self.rows = {}
        self.creates = []
        self.gets = []

    async def create_invoice(self, order, *, idempotence_key):
        self.creates.append(idempotence_key)
        row = self.rows.setdefault(order["id"], PartnerPayment(
            "payment-" + order["id"], order["id"], order["project_id"], order["tg_id"],
            order["price_kopecks"], "RUB", "pending", False,
            "https://pay.example.test/" + order["id"],
        ))
        return row

    async def get_payment(self, payment_id):
        self.gets.append(payment_id)
        return next(row for row in self.rows.values() if row.id == payment_id)

    def paid(self, order_id):
        self.rows[order_id] = replace(self.rows[order_id], status="succeeded", paid=True)


class Provisioner:
    def __init__(self):
        self.calls = []
        self.fail_once = False

    async def ensure_subscription(self, activation):
        self.calls.append(dict(activation))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("fake remote outage, not production")
        return ProvisionedSubscription("remote-" + activation["subscription_id"],
            activation["remote_username"], "https://sub.example.test/" + activation["subscription_id"])


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Isolated PostgreSQL/WASM module is not configured")
class PartnerCommerceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        await self.sql.query((Path(__file__).resolve().parents[1] / "ops/partner-platform/commerce.sql").read_text(), exec=True)
        self.registry = PartnerStore(lambda: self.sql)
        self.commerce = PartnerCommerceStore(lambda: self.sql)
        self.project = await self.registry.create_project(123, "integration-test", "own-brand", "Своя марка")
        self.pid = self.project["id"]
        await self.registry.save_bot_connection(self.pid, 123, 1, 200002, "PartnerClientBot", "cipher:" + "x" * 100)
        # Explicit local fixture only. There is NO public/manual activation API.
        await self.sql.execute("UPDATE wl_projects SET status='active' WHERE id=$1", self.pid)
        self.project = await self.registry.get_runtime_project(self.pid)
        self.payments, self.remote = Payments(), Provisioner()
        self.checkout = PartnerCheckoutService(self.commerce, self.payments, self.remote)
        app = FastAPI()
        app.include_router(create_router(self.registry, PartnerSettings(True, "partners.example.test", MANAGER),
                                         commerce_store=self.commerce, addon_loader=AsyncMock(return_value={})))
        self.webhook_key = b"local-test-webhook-key-with-32-bytes"
        app.include_router(create_client_router(self.registry.get_runtime_project,
            PartnerBotHandler(self.commerce, self.checkout), enabled=True, secret_key=self.webhook_key))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                                   base_url="https://offline.test"))
        self.update_id = 1

    async def update(self, data=None, *, tg=100, text=None):
        self.update_id += 1
        sender = {"id": tg, "is_bot": False}
        chat = {"id": tg, "type": "private"}
        if text is not None:
            update = {"update_id": self.update_id, "message": {"from": sender, "chat": chat, "text": text}}
        else:
            update = {"update_id": self.update_id, "callback_query": {"id": str(self.update_id), "from": sender,
                "data": data, "message": {"message_id": 1, "chat": chat,
                    "from": {"id": 200002, "is_bot": True}}}}
        response = await self.client.post(f"/partner-bots/{self.pid}/webhook", json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret(self.webhook_key, self.pid, 200002)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def buy(self, *, tg=100):
        await self.update(tg=tg, text="/start")
        review = await self.update("buy", tg=tg)
        callback = review["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        response = await self.update(callback, tg=tg)
        order = await self.commerce.get_pending_order(self.pid, tg)
        return response, order, callback

    async def admin(self, resource, *, owner=123):
        return await self.client.get(f"/partners/api/projects/{self.pid}/{resource}",
                                     headers={"Authorization": authorization(user_id=owner)})

    async def test_bot_purchase_verified_payment_ledger_and_real_owner_reports(self):
        empty = await self.admin("overview")
        self.assertEqual(empty.json()["customers"], 0)
        invoice_screen, order, callback = await self.buy()
        self.assertIn("Оплатить", str(invoice_screen))
        self.assertEqual((await self.commerce.list_subscriptions(self.pid, 100))[0]["expires_at"], None)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 0)
        await self.update(callback)  # same button after lost response
        self.assertEqual(len(self.payments.creates), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_orders"), 1)
        self.payments.paid(order["id"])
        check = next(button["callback_data"] for row in invoice_screen["reply_markup"]["inline_keyboard"]
                     for button in row if button.get("callback_data", "").startswith("check:"))
        activated = await self.update(check)
        self.assertIn("Подписка активирована", activated["text"])
        await self.update(check)
        sub = (await self.commerce.list_subscriptions(self.pid, 100))[0]
        self.assertEqual(sub["traffic_limit_bytes"], 300 * 1024**3)
        self.assertEqual(sub["device_limit"], 3)
        self.assertEqual(len(self.remote.calls), 1)
        report = (await self.admin("overview")).json()
        self.assertEqual(report["customers"], 1)
        self.assertEqual(report["active_subscriptions"], 1)
        self.assertEqual(report["periods"]["today"], {"sales": 1, "revenue_kopecks": 19900,
            "partner_kopecks": 13930, "platform_kopecks": 5970})
        self.assertFalse(report["sales_enabled"])
        orders = (await self.admin("orders")).json()["items"]
        self.assertEqual(orders[0]["status"], "active")
        self.assertNotIn("provider_payment_id", orders[0])
        self.assertNotIn("confirmation_url", orders[0])
        self.assertNotIn("subscription_url", (await self.admin("customers")).text)

    async def test_unpaid_reserved_slot_has_valid_customer_state_and_no_fake_purchase(self):
        await self.buy()
        response = await self.admin("customers")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["subscription_status"], "none")
        report = (await self.admin("overview")).json()
        self.assertEqual(report["active_subscriptions"], 0)
        self.assertEqual(report["periods"]["today"]["sales"], 0)
        for resource in ("overview", "customers", "orders"):
            self.assertEqual((await self.admin(resource, owner=456)).status_code, 404)
        self.assertEqual((await self.client.get(f"/partners/api/projects/{self.pid}/orders")).status_code, 401)

    async def test_restart_after_remote_success_before_completion_does_not_duplicate_money_or_days(self):
        _, order, _ = await self.buy()
        self.payments.paid(order["id"])
        # The fake remote records the absolute state before losing its response.
        # A restart must safely re-ensure that same state, not increment it.
        self.remote.fail_once = True
        pending = await self.checkout.reconcile(self.pid, 100, order["id"])
        self.assertEqual(pending["status"], "paid_pending_activation")
        reserved = await self.commerce.get_activation(self.pid, order["id"])
        restarted = PartnerCheckoutService(PartnerCommerceStore(lambda: self.sql), None, self.remote)
        result = await restarted.reconcile(self.pid, 100, order["id"])
        self.assertEqual(result["status"], "active")
        sub = (await self.commerce.list_subscriptions(self.pid, 100))[0]
        self.assertEqual(sub["expires_at"], reserved["expires_at"])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_customer_outbox"), 1)
        self.assertEqual(len(self.remote.calls), 2)
        self.assertEqual(self.remote.calls[0]["expires_at"], self.remote.calls[1]["expires_at"])

    async def test_paid_renewal_waits_for_older_activation_and_old_retry_cannot_shorten(self):
        _, first, _ = await self.buy()
        self.remote.fail_once = True
        self.payments.paid(first["id"])
        self.assertEqual((await self.checkout.reconcile(self.pid, 100, first["id"]))["status"], "paid_pending_activation")
        second = await self.commerce.create_order(self.pid, 100, str(uuid4()), 19900,
                                                   subscription_id=first["subscription_id"])
        await self.checkout.create_checkout(self.pid, 100, second["id"])
        self.payments.paid(second["id"])
        self.assertEqual((await self.checkout.reconcile(self.pid, 100, second["id"]))["status"], "paid_pending_activation")
        self.assertEqual(len(self.remote.calls), 1)  # newer remote ensure was blocked
        await self.checkout.reconcile(self.pid, 100, first["id"])
        await self.checkout.reconcile(self.pid, 100, second["id"])
        latest = (await self.commerce.list_subscriptions(self.pid, 100))[0]["expires_at"]
        count = len(self.remote.calls)
        await self.checkout.reconcile(self.pid, 100, first["id"])
        self.assertEqual(len(self.remote.calls), count)
        self.assertEqual((await self.commerce.list_subscriptions(self.pid, 100))[0]["expires_at"], latest)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 2)

    async def test_foreign_customer_order_is_rejected_before_provider_lookup(self):
        _, order, _ = await self.buy()
        before = len(self.payments.gets)
        with self.assertRaises(LookupError):
            await self.checkout.reconcile(self.pid, 999, order["id"])
        with self.assertRaises(LookupError):
            await self.checkout.create_checkout(str(uuid4()), 100, order["id"])
        self.assertEqual(len(self.payments.gets), before)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 0)

    async def test_canonical_canceled_payment_reopens_first_purchase_without_extra_slot_or_ledger(self):
        screen, order, _ = await self.buy()
        self.payments.rows[order["id"]] = replace(self.payments.rows[order["id"]], status="canceled", paid=False)
        check = next(button["callback_data"] for row in screen["reply_markup"]["inline_keyboard"]
                     for button in row if button.get("callback_data", "").startswith("check:"))
        canceled = await self.update(check)
        self.assertIn("Платёж отменён", canceled["text"])
        self.assertNotIn("url", str(canceled["reply_markup"]))
        _, fresh, _ = await self.buy()
        self.assertNotEqual(fresh["id"], order["id"])
        self.assertEqual(fresh["subscription_id"], order["subscription_id"])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_subscriptions"), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 0)

    async def test_explicit_recovery_pass_reconciles_saved_invoice_without_customer_button(self):
        _, order, _ = await self.buy()
        self.payments.paid(order["id"])
        result = await recover_partner_orders_once(self.commerce, self.checkout)
        self.assertEqual(result, {"checked": 1, "active": 1, "pending": 0, "canceled": 0, "errors": 0})
        self.assertEqual((await recover_partner_orders_once(self.commerce, self.checkout))["checked"], 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 1)
