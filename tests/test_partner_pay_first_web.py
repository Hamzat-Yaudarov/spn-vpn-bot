"""Payment-first central HTTP contracts; no real provider, bot or database."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from fastapi import FastAPI

from partner_web import create_router
from services.partner_enrollment_store import EnrollmentConflict
from services.partner_entry_pricing import EntryPricingPolicy
from services.partner_security import PartnerSettings
from test_partner_owner_pilot_web import saved_order
from test_partner_web import MANAGER, TENANT, authorization, project


class PayFirstHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.project = project(owner_tg_id=42, title="Мой сервис", bot={"id": None, "username": None})
        self.order = saved_order(owner=42, price=999000, bot_id=None,
                                 provider_payment_id=None, confirmation_url="")
        self.registry = AsyncMock()
        self.registry.prepare_enrollment_project.return_value = self.project
        self.entry = AsyncMock()
        self.entry.entry_pricing = EntryPricingPolicy(public_sales_enabled=True)
        self.entry.payment_first_enabled = True
        self.entry.get_current_entry_order.return_value = None
        self.entry.create_entry_order.return_value = self.order
        self.invoiced = {**self.order, "provider_payment_id": "one-receipt",
                         "confirmation_url": "https://pay.example.test/one-receipt"}
        self.entry.get_entry_order.return_value = self.invoiced
        self.checkout = SimpleNamespace(payments_available=True, launch_available=True,
            create_checkout=AsyncMock(return_value={"private_token": "must-not-leak"}))
        self.managed = SimpleNamespace(enabled=True, available=True)
        self.body = {"request_key": str(uuid4()), "expected_fee_kopecks": 999000}
        self.client = await self.make_client()

    async def make_client(self, surface="enrollment", allowlist=None):
        app = FastAPI()
        base = "/partners/workspace/{workspace_project_id}" if surface == "workspace" else "/partners"
        app.include_router(create_router(self.registry,
            PartnerSettings(True, "partners.example.test", MANAGER),
            enrollment_store=self.entry, entry_checkout=self.checkout,
            managed_service=self.managed, surface=surface, route_base=base,
            manager_allowed_user_ids=allowlist))
        return await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://offline.test",
            headers={"authorization": authorization(42)}))

    async def post(self, **kwargs):
        return await self.client.post("/partners/api/enrollment/checkout", json=self.body, **kwargs)

    async def test_one_action_prepares_server_defaults_and_one_saved_invoice(self):
        response = await self.post()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.registry.prepare_enrollment_project.assert_awaited_once_with(42, self.body["request_key"])
        self.entry.create_entry_order.assert_awaited_once_with(self.project["id"], 42,
            self.body["request_key"], 999000)
        self.checkout.create_checkout.assert_awaited_once_with(self.project["id"], 42, str(self.order["id"]))
        self.assertEqual(result["order"]["confirmation_url"], self.invoiced["confirmation_url"])
        self.assertEqual(result["project"]["id"], self.project["id"])
        self.assertTrue(result["pay_first_enabled"])
        self.assertNotIn("must-not-leak", response.text)
        self.assertIn("no-store", response.headers["cache-control"])

    async def test_paid_ten_ruble_order_is_recovered_without_another_payment(self):
        self.entry.get_current_entry_order.return_value = {**self.order,
            "status": "paid_pending_launch", "price_kopecks": 1000}
        response = await self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["order"]["price_kopecks"], 1000)
        self.assertEqual(response.json()["launch_state"], "awaiting_bot")
        self.assertTrue(response.json()["order"]["creation_available"])
        self.checkout.create_checkout.assert_not_awaited()
        self.entry.create_entry_order.assert_not_awaited()

    async def test_launched_order_is_not_charged_again(self):
        self.project["status"] = "active"
        self.entry.get_current_entry_order.return_value = {**self.order, "status": "launched", "bot_id": 777}
        response = await self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["project"]["workspace_available"])
        self.checkout.create_checkout.assert_not_awaited()
        self.entry.create_entry_order.assert_not_awaited()

    async def test_suspended_or_closed_service_does_not_offer_another_charge(self):
        for status in ("suspended", "closed"):
            self.project["status"] = status
            self.entry.get_current_entry_order.return_value = {**self.order, "status": "launched", "bot_id": 777}
            self.assertEqual((await self.post()).status_code, 403)
        self.checkout.create_checkout.assert_not_awaited()
        self.entry.create_entry_order.assert_not_awaited()

    async def test_retries_and_different_tab_keys_converge_on_current_order(self):
        self.entry.create_entry_order.side_effect = EnrollmentConflict("another tab won")
        self.entry.get_current_entry_order.side_effect = [None, self.invoiced]
        response = await self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.checkout.create_checkout.assert_awaited_once_with(self.project["id"], 42, str(self.order["id"]))

    async def test_authentication_and_allowlist_precede_any_project_preparation(self):
        for proof in ("", "tma invalid", authorization(42, token=TENANT)):
            response = await self.post(headers={"authorization": proof})
            self.assertEqual(response.status_code, 401)
        restricted = await self.make_client(allowlist=[123])
        response = await restricted.post("/partners/api/enrollment/checkout", json=self.body)
        self.assertEqual(response.status_code, 403)
        self.registry.prepare_enrollment_project.assert_not_awaited()

    async def test_stale_fee_bool_and_untrusted_payload_are_rejected_before_project(self):
        for changes in ({"expected_fee_kopecks": 1000}, {"expected_fee_kopecks": True},
                        {"request_key": "invalid"}, {"owner_tg_id": 42},
                        {"project_id": self.project["id"]}, {"title": "forged"},
                        {"price": "10"}, {"bot_id": 7}):
            response = await self.client.post("/partners/api/enrollment/checkout", json={**self.body, **changes})
            self.assertIn(response.status_code, (400, 409), changes)
        self.registry.prepare_enrollment_project.assert_not_awaited()

    async def test_disabled_payment_or_telegram_never_creates_a_project(self):
        self.checkout.payments_available = False
        self.assertEqual((await self.post()).status_code, 503)
        self.checkout.payments_available = True
        self.managed.available = False
        self.assertEqual((await self.post()).status_code, 503)
        self.registry.prepare_enrollment_project.assert_not_awaited()

    async def test_legacy_route_is_disabled_and_workspace_route_absent(self):
        legacy = await self.make_client("legacy")
        self.assertEqual((await legacy.post("/partners/api/enrollment/checkout", json=self.body)).status_code, 404)
        workspace = await self.make_client("workspace")
        self.assertEqual((await workspace.post(
            f"/partners/workspace/{self.project['id']}/api/enrollment/checkout", json=self.body)).status_code, 404)
        self.registry.prepare_enrollment_project.assert_not_awaited()

    async def test_manual_draft_creation_is_closed_before_body_and_registry(self):
        response = await self.client.post("/partners/api/projects", content="not-json")
        self.assertEqual(response.status_code, 404)
        self.registry.create_project.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
