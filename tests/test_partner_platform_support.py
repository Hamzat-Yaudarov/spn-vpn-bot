"""Offline support: central proof, explicit actor audit, no owner impersonation."""

import base64
import json
import unittest
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from partner_support_web import create_support_router
from services.partner_manager import PartnerManagerHandler
from services.partner_entry_pricing import EntryPricingPolicy
from services.partner_security import PartnerSettings
from services.partner_store import InvalidProject, ProjectNotFound, ProjectConflict
from services.partner_support import PartnerSupportStore
from test_partner_store import RecordingConnection, project_row, PROJECT_ID
from test_partner_web import MANAGER, TENANT, authorization, project
from test_partner_runtime_manager import BOT_ID, ORIGIN, update


OPERATOR = 6910097562
PID = str(PROJECT_ID)


class SupportStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_or_unknown_operator_denied_before_database(self):
        getter = AsyncMock()
        for allowed, actor in (((), OPERATOR), ((OPERATOR,), 123)):
            store = PartnerSupportStore(getter, operator_ids=allowed)
            for operation in (
                store.list_projects(actor), store.get_project(PID, actor),
                store.update_project(PID, actor, 1, {"title": "Test"}),
            ):
                with self.assertRaises(ProjectNotFound):
                    await operation
        getter.assert_not_called()

    async def test_list_and_read_use_public_columns_without_credentials(self):
        conn = RecordingConnection(rows=[project_row()], many=[project_row()])
        store = PartnerSupportStore(lambda: conn, operator_ids=[OPERATOR])
        listing = await store.list_projects(OPERATOR)
        read = await store.get_project(PID, OPERATOR)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(read["owner_tg_id"], 42)
        self.assertNotIn("secret-must-never-leak", str((read, listing)))
        for _, sql, _ in conn.calls:
            self.assertNotIn("encrypted_token", sql)
            self.assertNotIn("SELECT *", sql)

    async def test_update_keeps_real_owner_and_audits_actual_operator_atomically(self):
        conn = RecordingConnection(rows=[project_row(version=2)])
        store = PartnerSupportStore(lambda: conn, operator_ids=[OPERATOR])
        result = await store.update_project(PID, OPERATOR, 1, {"title": "Assisted"})
        self.assertTrue(conn.committed)
        self.assertEqual(result["owner_tg_id"], 42)
        update_query = conn.calls[0]
        self.assertIn("WHERE id=$1 AND version=$2", update_query[1])
        self.assertNotIn("owner_tg_id=", update_query[1])
        self.assertEqual(update_query[2], (PROJECT_ID, 1, "Assisted"))
        audit = conn.calls[-1]
        self.assertEqual(audit[2][:3], (PROJECT_ID, 42, 2))
        self.assertEqual(json.loads(audit[2][3]), {
            "fields": ["title"], "actor_tg_id": OPERATOR, "context": "platform_support",
        })

    async def test_audit_failure_rolls_back_and_newer_version_is_not_overwritten(self):
        conn = RecordingConnection(rows=[project_row(version=2)], execute_error=RuntimeError("private"))
        store = PartnerSupportStore(lambda: conn, operator_ids=[OPERATOR])
        with self.assertRaises(RuntimeError):
            await store.update_project(PID, OPERATOR, 1, {"title": "Assisted"})
        self.assertTrue(conn.rolled_back)
        stale = RecordingConnection(rows=[None], value=1)
        with self.assertRaises(ProjectConflict):
            await PartnerSupportStore(lambda: stale, operator_ids=[OPERATOR]).update_project(PID, OPERATOR, 1, {"title": "Stale"})
        self.assertTrue(stale.rolled_back)

    async def test_no_token_ownership_financial_or_free_grant_mutations(self):
        getter = AsyncMock()
        store = PartnerSupportStore(getter, operator_ids=[OPERATOR])
        for field in ("token", "encrypted_token", "owner_tg_id", "status", "balance", "expires_at", "bot_id"):
            with self.subTest(field=field), self.assertRaises(InvalidProject):
                await store.update_project(PID, OPERATOR, 1, {field: "forbidden"})
        with self.assertRaises(InvalidProject):
            await store.update_project(PID, OPERATOR, 1, {"price_kopecks": 100})
        getter.assert_not_called()


class SupportWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = AsyncMock()
        self.store.require_operator = lambda actor: actor
        self.store.list_projects.return_value = {"items": [project()], "truncated": False}
        self.store.get_project.return_value = project()
        self.store.update_project.return_value = project(version=2)
        settings = PartnerSettings(True, "partners.example.com", MANAGER, (base64.urlsafe_b64encode(b"w" * 32).decode(),))
        app = FastAPI()
        app.include_router(create_support_router(self.store, settings, operator_ids=[OPERATOR]))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://manager.example.com"))

    def headers(self, actor=OPERATOR, token=MANAGER):
        return {"authorization": authorization(actor, token=token)}

    async def test_only_central_signed_operator_can_list_all_projects(self):
        for actor, token, status in ((OPERATOR, TENANT, 401), (123, MANAGER, 403)):
            response = await self.client.get("/partners/support/api/projects", headers=self.headers(actor, token))
            self.assertEqual(response.status_code, status)
        self.store.list_projects.assert_not_awaited()
        response = await self.client.get("/partners/support/api/projects", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        self.store.list_projects.assert_awaited_once_with(OPERATOR, cursor=0)

    async def test_safe_edit_uses_real_actor_not_owner_and_minimum_price(self):
        path = "/partners/support/api/projects/" + PID
        response = await self.client.patch(path, headers=self.headers(), json={"expected_version": 1, "title": "Help", "price": "219.00"})
        self.assertEqual(response.status_code, 200, response.text)
        self.store.update_project.assert_awaited_once_with(PID, OPERATOR, 1, {"title": "Help", "price_kopecks": 21900})
        for value in ({"expected_version": 1, "owner_tg_id": OPERATOR}, {"expected_version": 1, "price": "10.00"}):
            self.assertEqual((await self.client.patch(path, headers=self.headers(), json=value)).status_code, 400)

    async def test_finance_grants_connections_and_messaging_are_not_mounted(self):
        for suffix in ("/bot", "/entry", "/finance/withdrawals", "/management/team", "/subscriptions", "/broadcasts"):
            response = await self.client.post("/partners/support/api/projects/" + PID + suffix, headers=self.headers(), json={})
            self.assertEqual(response.status_code, 404)

    async def test_storage_errors_are_redacted(self):
        self.store.get_project.side_effect = RuntimeError("password-private")
        response = await self.client.get("/partners/support/api/projects/" + PID, headers=self.headers())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("password-private", response.text)


class SupportManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_real_operator_sees_support_button_and_project_deeplink(self):
        handler = PartnerManagerHandler(BOT_ID, ORIGIN, operator_ids=[42])
        command = await handler.handle(update())
        self.assertEqual(command["reply_markup"]["inline_keyboard"][-1][0]["web_app"]["url"], ORIGIN + "/partners/support")
        incoming = update()
        incoming["message"]["text"] = "/start support_" + PID
        response = await handler.handle(incoming)
        self.assertEqual(response["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"], ORIGIN + "/partners/support?project=" + PID)
        response = await PartnerManagerHandler(BOT_ID, ORIGIN).handle(incoming)
        self.assertNotIn("/support", str(response))

    async def test_public_trial_manager_copy_matches_backend_price(self):
        handler = PartnerManagerHandler(BOT_ID, ORIGIN, entry_pricing=EntryPricingPolicy(public_trial_enabled=True))
        command = await handler.handle(update())
        self.assertIn("Вступление — 10 ₽ один раз.", command["text"])
        self.assertNotIn("9 990", command["text"])
