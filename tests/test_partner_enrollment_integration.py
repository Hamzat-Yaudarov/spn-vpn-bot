"""Owner HTTP -> fee -> verified launch -> client webhook on isolated PG.

All merchant and Telegram adapters are explicit local fakes. No .env, real
network, production database, Telegram write or money transfer is used.
"""
from dataclasses import replace
import os
from pathlib import Path
import unittest
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import FastAPI
import httpx

from partner_client_web import create_client_router, webhook_secret
from partner_web import create_router
from services.partner_bot import PartnerBotHandler
from services.partner_checkout import PartnerPayment
from services.partner_commerce_store import PartnerCommerceStore
from services.partner_enrollment_store import PartnerEnrollmentStore
from services.partner_entry_checkout import PartnerEntryCheckoutService, recover_partner_entries_once
from services.partner_launcher import PartnerLaunchService
from services.partner_security import PartnerSettings, TokenVault
from services.partner_store import PartnerStore
from test_partner_store_postgres import RegistryBridge
from test_partner_web import MANAGER, TENANT, authorization


class FeePayments:
    def __init__(self):
        self.rows, self.creates, self.gets = {}, [], []

    async def create_invoice(self, order, *, idempotence_key):
        self.creates.append((idempotence_key, order["price_kopecks"], order["purpose"]))
        return self.rows.setdefault(order["id"], PartnerPayment("entry-" + order["id"], order["id"],
            order["project_id"], order["tg_id"], order["price_kopecks"], "RUB", "pending", False,
            "https://pay.example.test/" + order["id"], purpose="entry"))

    async def get_payment(self, payment_id):
        self.gets.append(payment_id)
        return next(payment for payment in self.rows.values() if payment.id == payment_id)

    def paid(self, order):
        self.rows[order] = replace(self.rows[order], status="succeeded", paid=True)


class Telegram:
    def __init__(self):
        self.url = ""
        self.allowed_updates = []
        self.calls = []
        self.fail_after_set_once = False
        self.secret = None

    async def get_me(self, token):
        self.calls.append("getMe")
        assert token == TENANT
        return {"id": 200002, "is_bot": True, "username": "MyPartnerBot"}

    async def get_webhook_info(self, token):
        self.calls.append("getWebhookInfo")
        return {"url": self.url, "allowed_updates": list(self.allowed_updates)}

    async def set_webhook(self, token, *, url, secret_token):
        self.calls.append("setWebhook")
        self.url, self.secret = url, secret_token
        self.allowed_updates = ["message", "callback_query"]
        if self.fail_after_set_once:
            self.fail_after_set_once = False
            raise RuntimeError("response lost after Telegram accepted the webhook")


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Isolated PostgreSQL/WASM module is not configured")
class EnrollmentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql = await RegistryBridge().start()
        self.addAsyncCleanup(self.sql.close)
        base = Path(__file__).resolve().parents[1] / "ops/partner-platform"
        for filename in ("commerce.sql", "enrollment.sql"):
            await self.sql.query((base / filename).read_text(), exec=True)
        self.registry, self.commerce = PartnerStore(lambda: self.sql), PartnerCommerceStore(lambda: self.sql)
        self.store = PartnerEnrollmentStore(lambda: self.sql)
        self.vault = TokenVault((Fernet.generate_key().decode(),))
        self.project = await self.registry.create_project(123, "entry-integration", "own-entry", "Мой VPN")
        self.pid = self.project["id"]
        self.cipher = self.vault.encrypt(self.pid, 200002, TENANT)
        self.project = await self.registry.save_bot_connection(self.pid, 123, 1, 200002, "MyPartnerBot", self.cipher)
        self.provider, self.telegram = FeePayments(), Telegram()
        self.key = b"local-launch-key-of-more-than-32-bytes"
        self.launcher = PartnerLaunchService(self.store, self.vault, self.telegram,
            webhook_base_url="https://bots.example.test", webhook_secret_key=self.key, clients_enabled=True)
        self.service = PartnerEntryCheckoutService(self.store, self.provider, self.launcher)
        self.client = await self.client_for(self.service)
        self.entry = f"/partners/api/projects/{self.pid}/entry"

    async def client_for(self, service):
        app = FastAPI()
        app.include_router(create_router(self.registry, PartnerSettings(True, "partners.example.test", MANAGER),
                                         commerce_store=self.commerce, enrollment_store=self.store, entry_checkout=service))
        app.include_router(create_client_router(self.registry.get_runtime_project, PartnerBotHandler(self.commerce),
                                                enabled=True, secret_key=self.key))
        return await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="https://offline.test", headers={"Authorization": authorization(user_id=123)}))

    async def create_order(self, key=None):
        response = await self.client.post(self.entry, json={"request_key": key or str(uuid4()), "expected_fee_kopecks": 999000})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["order"]

    async def invoice(self):
        order = await self.create_order()
        response = await self.client.post(self.entry + f"/{order['id']}/checkout", json={})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["order"]

    async def check(self, order, client=None):
        return await (client or self.client).post(self.entry + f"/{order['id']}/check", json={})

    async def test_fee_once_activates_bot_only_after_canonical_payment_then_accepts_private_update(self):
        initial = await self.client.get(self.entry)
        self.assertIsNone(initial.json()["order"])
        self.assertTrue(initial.json()["payments_available"])
        order = await self.invoice()
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual((await self.registry.get_project(self.pid, 123))["status"], "draft")
        self.provider.paid(order["id"])
        response = await self.check(order)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["order"]["status"], "launched")
        active = await self.registry.get_runtime_project(self.pid)
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["version"], self.project["version"] + 1)
        self.assertEqual(self.provider.creates, [(order["id"], 999000, "entry")])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_revenue_ledger"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_subscriptions"), 0)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 1)
        self.assertEqual(self.telegram.secret, webhook_secret(self.key, self.pid, 200002))
        event = {"update_id": 1, "message": {"from": {"id": 100, "is_bot": False},
            "chat": {"id": 100, "type": "private"}, "text": "/start"}}
        screen = await self.client.post(f"/partner-bots/{self.pid}/webhook", json=event,
            headers={"X-Telegram-Bot-Api-Secret-Token": self.telegram.secret})
        self.assertEqual(screen.status_code, 200, screen.text)
        self.assertIn("Мой VPN", screen.json()["text"])
        calls = list(self.telegram.calls)
        await self.check(order)
        self.assertEqual(calls, self.telegram.calls)
        for secret in (TENANT, self.cipher, self.telegram.secret):
            self.assertNotIn(secret, response.text)
            self.assertNotIn(secret, (await self.client.get(self.entry)).text)

    async def test_lost_set_webhook_response_recovery_uses_paid_order_and_same_secret(self):
        order = await self.invoice()
        self.provider.paid(order["id"])
        self.telegram.fail_after_set_once = True
        response = await self.check(order)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["order"]["status"], "paid_pending_launch")
        self.assertEqual(response.json()["launch_state"], "retry")
        self.assertEqual((await self.registry.get_project(self.pid, 123))["status"], "draft")
        accepted_secret = self.telegram.secret
        restarted = PartnerEntryCheckoutService(PartnerEnrollmentStore(lambda: self.sql), None, self.launcher)
        other_client = await self.client_for(restarted)
        response = await self.check(order, other_client)
        self.assertEqual(response.json()["order"]["status"], "launched")
        self.assertEqual(self.telegram.secret, accepted_secret)
        self.assertEqual(self.telegram.calls.count("setWebhook"), 2)
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 1)

    async def test_foreign_webhook_is_not_replaced_without_explicit_owner_consent(self):
        self.telegram.url = "https://old.example.test/private-token-must-not-echo"
        order = await self.invoice()
        self.provider.paid(order["id"])
        response = await self.check(order)
        self.assertEqual(response.json()["launch_state"], "needs_webhook_replace")
        self.assertEqual(self.telegram.calls.count("setWebhook"), 0)
        restored = await self.client.get(self.entry)
        self.assertEqual(restored.json()["launch_state"], "needs_webhook_replace")
        self.assertNotIn("private-token", restored.text)
        self.assertEqual((await recover_partner_entries_once(self.store, self.service))["checked"], 0)
        path = self.entry + f"/{order['id']}/launch"
        self.assertEqual((await self.client.post(path, json={"replace_existing": "true"})).status_code, 400)
        result = await self.client.post(path, json={"replace_existing": True})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["launch_state"], "launched")
        self.assertEqual(self.telegram.calls.count("setWebhook"), 1)
        self.assertEqual(len(self.provider.creates), 1)

    async def test_disabled_installation_does_not_create_fee_and_spoofed_paid_body_is_forbidden(self):
        disabled = await self.client_for(None)
        response = await disabled.post(self.entry, json={"request_key": str(uuid4()), "expected_fee_kopecks": 999000})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_entry_orders"), 0)
        wrong_fee = await self.client.post(self.entry, json={"request_key": str(uuid4()), "expected_fee_kopecks": 99900})
        self.assertEqual(wrong_fee.status_code, 409)
        order = await self.invoice()
        spoof = await self.client.post(self.entry + f"/{order['id']}/check", json={"paid": True})
        self.assertEqual(spoof.status_code, 400)
        unpaid_launch = await self.client.post(self.entry + f"/{order['id']}/launch", json={"replace_existing": False})
        self.assertNotEqual(unpaid_launch.status_code, 200)
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.provider.gets, [])

    async def test_entry_purpose_wrong_amount_and_owner_do_not_activate_or_reach_provider(self):
        order = await self.invoice()
        self.provider.rows[order["id"]] = replace(self.provider.rows[order["id"]], status="succeeded", paid=True,
                                                purpose="subscription")
        response = await self.check(order)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_launch_jobs"), 0)
        self.assertEqual(self.telegram.calls, [])
        before = len(self.provider.gets)
        response = await self.client.post(self.entry + f"/{order['id']}/check", json={},
                                          headers={"Authorization": authorization(user_id=456)})
        self.assertEqual(response.status_code, 404)
        response = await self.client.post(self.entry + f"/{uuid4()}/check", json={})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.provider.gets), before)

    async def test_ambiguous_database_completion_rolls_back_project_activation_and_can_retry(self):
        order = await self.invoice()
        self.provider.paid(order["id"])
        original = self.sql.fetchrow
        failed = False

        async def fail_once(sql, *args):
            nonlocal failed
            if "UPDATE wl_entry_orders SET status='launched'" in sql and not failed:
                failed = True
                raise RuntimeError("private database password, injected outage")
            return await original(sql, *args)

        self.sql.fetchrow = fail_once
        response = await self.check(order)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("password", response.text)
        self.assertEqual((await self.registry.get_project(self.pid, 123))["status"], "draft")
        self.assertEqual((await self.store.get_entry_order(self.pid, 123, order["id"]))["status"], "paid_pending_launch")
        self.sql.fetchrow = original
        result = await self.check(order)
        self.assertEqual(result.json()["order"]["status"], "launched")
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 1)

    async def test_bot_identity_is_locked_but_same_bot_token_rotation_is_allowed(self):
        await self.invoice()
        version = self.project["version"]
        from services.partner_store import ProjectConflict
        with self.assertRaises(ProjectConflict):
            await self.registry.save_bot_connection(self.pid, 123, version, 300003, "OtherBot", "x" * 100)
        current = await self.registry.get_project(self.pid, 123)
        self.assertEqual(current["version"], version)
        rotated = self.vault.encrypt(self.pid, 200002, TENANT)
        updated = await self.registry.save_bot_connection(self.pid, 123, version, 200002, "MyPartnerBot", rotated)
        self.assertEqual(updated["version"], version + 1)

    async def test_suspended_project_cannot_receive_first_entry_invoice_from_saved_order(self):
        order = await self.create_order()
        await self.sql.execute("UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid)
        response = await self.client.post(self.entry + f"/{order['id']}/checkout", json={})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual((await self.store.get_entry_order(self.pid, 123, order["id"]))["status"], "created")

    async def test_paused_runtime_hides_unpaid_link_but_canonical_paid_invoice_is_reconciled(self):
        order = await self.invoice()
        disabled = await self.client_for(None)
        paused = await disabled.get(self.entry)
        self.assertFalse(paused.json()["payments_available"])
        self.assertNotIn("confirmation_url", paused.json()["order"])
        await self.sql.execute("UPDATE wl_projects SET status='suspended' WHERE id=$1", self.pid)
        status = await self.client.get(self.entry)
        self.assertFalse(status.json()["payments_available"])
        self.assertNotIn("confirmation_url", status.json()["order"])
        self.provider.paid(order["id"])
        checked = await self.check(order)
        self.assertEqual(checked.status_code, 409)
        retained = await self.store.get_entry_order(self.pid, 123, order["id"])
        self.assertEqual(retained["status"], "paid_pending_launch")
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(await self.sql.fetchval("SELECT count(*) FROM wl_partner_outbox"), 1)


if __name__ == "__main__":
    unittest.main()
