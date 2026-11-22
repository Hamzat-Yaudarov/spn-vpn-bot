"""Signed owner HTTP pricing with real store policy and offline SQL-recording port."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI

from partner_web import create_router
from services.partner_enrollment_store import PartnerEnrollmentStore
from services.partner_entry_pricing import EntryPricingPolicy, ENTRY_PILOT_OWNER_ID
from services.partner_security import PartnerSettings
from services.partner_store import ProjectNotFound
from test_partner_store import RecordingConnection
from test_partner_web import MANAGER, TENANT, authorization, project


OWNER = ENTRY_PILOT_OWNER_ID
NORMAL, PILOT = 999000, 1000
PID = project()["id"]
NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def saved_order(*, owner=OWNER, price=PILOT, key=None, **changes):
    return {"id": uuid4(), "project_id": UUID(PID), "owner_tg_id": owner,
            "request_key": key or uuid4(), "bot_id": 200002, "purpose": "entry",
            "price_kopecks": price, "currency": "RUB", "status": "created",
            "provider_payment_id": "saved-entry-invoice", "confirmation_url": "https://pay.example.test/saved-entry",
            "created_at": NOW, "paid_at": None, "launched_at": None, **changes}


class OwnerPilotHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = RecordingConnection()
        self.registry = AsyncMock()
        self.project = project(owner_tg_id=OWNER, bot={"id": 200002, "username": "OwnedBot"})

        async def get_project(pid, actor):
            if str(pid) != PID or actor != self.project["owner_tg_id"]:
                raise ProjectNotFound()
            return dict(self.project)
        self.registry.get_project.side_effect = get_project

    async def client(self, *, enabled=True, actor=OWNER, project_owner=OWNER, public_trial=False):
        self.project["owner_tg_id"] = project_owner
        store = PartnerEnrollmentStore(lambda: self.connection, entry_pricing=EntryPricingPolicy(enabled, public_trial))
        app = FastAPI()
        app.include_router(create_router(
            self.registry, PartnerSettings(True, "partners.example.test", MANAGER),
            surface="enrollment", enrollment_store=store,
            entry_checkout=SimpleNamespace(payments_available=True, launch_available=True),
        ))
        return await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://offline.test",
            headers={"authorization": authorization(actor)},
        ))

    @property
    def entry(self): return f"/partners/api/projects/{PID}/entry"

    def sql_project(self):
        return {"id": UUID(PID), "owner_tg_id": self.project["owner_tg_id"], "bot_id": 200002, "status": "draft"}

    def new_order_rows(self):
        def inserted(sql, args):
            return saved_order(id=args[0], owner=args[2], key=args[3], price=args[5], provider_payment_id=None, confirmation_url="")
        return [self.sql_project(), None, inserted]

    async def test_settings_price_uses_verified_actor_and_flag_only(self):
        client = await self.client()
        for actor, price, pilot in ((OWNER, PILOT, True), (42, NORMAL, False), (OWNER + 1, NORMAL, False)):
            response = await client.get("/partners/api/settings", headers={"authorization": authorization(actor)})
            self.assertEqual(response.status_code, 200)
            self.assertEqual((response.json()["actor_id"], response.json()["entry_fee_kopecks"], response.json()["owner_entry_pilot"]), (actor, price, pilot))
        off = await self.client(enabled=False)
        response = await off.get("/partners/api/settings")
        self.assertEqual((response.json()["entry_fee_kopecks"], response.json()["owner_entry_pilot"]), (NORMAL, False))
        self.assertEqual(self.connection.calls, [])

    async def test_public_trial_settings_and_new_order_use_ten_rubles_for_nonowner(self):
        client = await self.client(enabled=False, public_trial=True, actor=42, project_owner=42)
        response = await client.get('/partners/api/settings')
        self.assertEqual((response.json()['entry_fee_kopecks'], response.json()['public_entry_trial'],
                          response.json()['owner_entry_pilot']), (1000, True, False))
        self.connection = RecordingConnection(rows=self.new_order_rows())
        response = await client.post(self.entry, json={'request_key': str(uuid4()), 'expected_fee_kopecks': 1000})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()['entry_fee_kopecks'], response.json()['public_entry_trial'],
                          response.json()['order']['price_kopecks']), (1000, True, 1000))

    async def test_public_trial_rejects_unconfirmed_old_fee_and_foreign_tenant_identity(self):
        client = await self.client(enabled=False, public_trial=True, actor=42, project_owner=42)
        self.connection = RecordingConnection(rows=[self.sql_project(), None])
        response = await client.post(self.entry, json={'request_key': str(uuid4()), 'expected_fee_kopecks': 999000})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any('INSERT INTO' in sql for _, sql, _ in self.connection.calls))
        response = await client.get('/partners/api/settings', headers={'authorization': authorization(42, token=TENANT)})
        self.assertEqual(response.status_code, 401)

    async def test_enabled_owner_and_other_owner_create_only_server_selected_fee(self):
        for actor, price in ((OWNER, PILOT), (42, NORMAL)):
            client = await self.client(actor=actor, project_owner=actor)
            self.connection = RecordingConnection(rows=self.new_order_rows())
            response = await client.post(self.entry, json={"request_key": str(uuid4()), "expected_fee_kopecks": price})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["order"]["price_kopecks"], price)
            self.assertEqual(response.json()["entry_fee_kopecks"], price)
            insert = next(call for call in self.connection.calls if "INSERT INTO wl_entry_orders" in call[1])
            self.assertEqual((insert[2][2], insert[2][5]), (actor, price))
            self.assertNotIn("owner_tg_id", response.json()["order"])

    async def test_spoofed_discount_against_normal_store_or_nonowner_is_409(self):
        for enabled, actor in ((False, OWNER), (True, 42)):
            client = await self.client(enabled=enabled, actor=actor, project_owner=actor)
            self.connection = RecordingConnection(rows=[self.sql_project(), None])
            response = await client.post(self.entry, json={"request_key": str(uuid4()), "expected_fee_kopecks": PILOT})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertFalse(any(sql.lstrip().startswith(("INSERT", "UPDATE")) for _, sql, _ in self.connection.calls))

    async def test_old_normal_fee_requires_new_confirmation_when_pilot_current(self):
        client = await self.client()
        self.connection = RecordingConnection(rows=[self.sql_project(), None])
        response = await client.post(self.entry, json={"request_key": str(uuid4()), "expected_fee_kopecks": NORMAL})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any("INSERT INTO" in sql for _, sql, _ in self.connection.calls))

    async def test_saved_key_retry_matches_immutable_price_across_flag_changes(self):
        for enabled, saved, expected, code in (
            (False, PILOT, PILOT, 200), (False, PILOT, NORMAL, 409),
            (True, NORMAL, NORMAL, 200), (True, NORMAL, PILOT, 409),
        ):
            client = await self.client(enabled=enabled)
            row = saved_order(price=saved)
            self.connection = RecordingConnection(rows=[self.sql_project(), row])
            response = await client.post(self.entry, json={"request_key": str(row["request_key"]), "expected_fee_kopecks": expected})
            self.assertEqual(response.status_code, code, response.text)
            if code == 200:
                self.assertEqual(response.json()["order"]["price_kopecks"], saved)
                self.assertEqual(response.json()["order"]["id"], str(row["id"]))
            self.assertFalse(any(sql.lstrip().startswith(("INSERT", "UPDATE")) for _, sql, _ in self.connection.calls))

    async def test_get_pending_pilot_keeps_saved_10_with_current_normal_fee_flag_off(self):
        client = await self.client(enabled=False)
        row = saved_order()
        self.connection = RecordingConnection(rows=[self.sql_project(), row])
        response = await client.get(self.entry)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual((data["entry_fee_kopecks"], data["owner_entry_pilot"]), (NORMAL, False))
        self.assertEqual((data["order"]["price_kopecks"], data["order"]["confirmation_url"]), (PILOT, row["confirmation_url"]))
        self.assertEqual(self.connection.calls[0][0], "fetchrow")
        self.assertFalse(any(op == "execute" for op, _, _ in self.connection.calls))

    async def test_foreign_project_or_actor_never_reaches_entry_store(self):
        client = await self.client()
        for path, actor in ((self.entry, 42), (f"/partners/api/projects/{uuid4()}/entry", OWNER)):
            response = await client.get(path, headers={"authorization": authorization(actor)})
            self.assertEqual(response.status_code, 404)
            response = await client.post(path, headers={"authorization": authorization(actor)}, json={"request_key": str(uuid4()), "expected_fee_kopecks": PILOT})
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.connection.calls, [])

    async def test_tenant_token_cannot_forge_pilot_owner_signature(self):
        client = await self.client()
        for path in ("/partners/api/settings", self.entry):
            response = await client.get(path, headers={"authorization": authorization(OWNER, token=TENANT)})
            self.assertEqual(response.status_code, 401)
        self.registry.get_project.assert_not_awaited()
        self.assertEqual(self.connection.calls, [])

    async def test_arbitrary_fee_types_and_forged_owner_fields_rejected_before_store(self):
        client = await self.client()
        for fee in (True, "1000", 1000.0, 1, 99900, -1):
            response = await client.post(self.entry, json={"request_key": str(uuid4()), "expected_fee_kopecks": fee})
            self.assertEqual(response.status_code, 409)
        for key in ("owner_tg_id", "owner_id", "entry_paid", "owner_entry_pilot"):
            response = await client.post(self.entry, json={"request_key": str(uuid4()), "expected_fee_kopecks": PILOT, key: OWNER})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.connection.calls, [])


if __name__ == "__main__":
    unittest.main()
