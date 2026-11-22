"""HTTP contract checks with injected trusted actors; no real Telegram/initData."""

import unittest
from uuid import uuid4

from fastapi import FastAPI, HTTPException
import httpx

from partner_management_web import create_management_router
from services.partner_management import ManagementDenied, ManagementConflict


class FakeStore:
    def __init__(self):
        self.identities = []
        self.calls = []

    async def record_manager_identity(self, actor):
        self.identities.append(actor)

    async def visible_projects(self, actor):
        return {"items": [{"id": "mine", "role": "owner"}], "truncated": False}

    async def customers(self, pid, actor, **kwargs):
        self.calls.append((pid, actor, kwargs))
        if pid == "foreign":
            raise ManagementDenied()
        return {"items": [], "next_cursor": None}

    async def save_campaign(self, pid, actor, **kwargs):
        self.calls.append((pid, actor, kwargs))
        if kwargs["title"] == "conflict":
            raise ManagementConflict("Обновите кампанию.")
        return {"id": "campaign", "title": kwargs["title"]}

    async def save_broadcast(self, pid, actor, **kwargs):
        self.calls.append((pid, actor, kwargs))
        return {"id": "draft", "status": "draft", "delivery_enabled": False}


class ManagementWebTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, store, authenticate, enabled=True):
        app = FastAPI()
        app.include_router(
            create_management_router(store, authenticate, enabled=enabled)
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://manager.example.org",
        )
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_disabled_and_unverified_actor_never_touch_identity(self):
        store = FakeStore()
        client = await self.client(store, lambda request: 42, enabled=False)
        self.assertEqual(
            (await client.get("/partners/api/management/projects")).status_code, 404
        )
        self.assertEqual(store.identities, [])
        for actor in (None, True, "42", 0):
            client = await self.client(store, lambda request: actor)
            response = await client.get("/partners/api/management/projects")
            self.assertEqual(response.status_code, 401)
            self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(store.identities, [])

    async def test_verified_actor_scope_and_private_error_headers(self):
        store = FakeStore()

        async def authenticate(request):
            return 42

        client = await self.client(store, authenticate)
        result = await client.get(
            "/partners/api/projects/mine/management/customers?query=123&limit=20&cursor=5"
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            store.calls[0], ("mine", 42, {"query": "123", "limit": 20, "cursor": 5})
        )
        response = await client.get(
            "/partners/api/projects/foreign/management/customers"
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    async def test_mutations_strict_fields_duplicate_keys_and_confirmation_not_implicit(
        self,
    ):
        store = FakeStore()
        client = await self.client(store, lambda request: 42)
        url = "/partners/api/projects/mine/management/broadcasts"
        valid = {"request_key": str(uuid4()), "text": "Новость"}
        result = await client.post(url, json=valid)
        self.assertEqual(result.json()["status"], "draft")
        self.assertFalse(result.json()["delivery_enabled"])
        self.assertEqual(len(store.calls), 1)
        for payload in (
            {**valid, "actor_id": 999},
            {**valid, "status": "queued"},
            {**valid, "recipients": [123]},
        ):
            self.assertEqual((await client.post(url, json=payload)).status_code, 400)
        self.assertEqual(
            (
                await client.post(
                    url,
                    content='{"text":"a","text":"b","request_key":"x"}',
                    headers={"Content-Type": "application/json"},
                )
            ).status_code,
            400,
        )
        self.assertEqual(
            (
                await client.post(
                    url,
                    content="x" * 17000,
                    headers={"Content-Type": "application/json"},
                )
            ).status_code,
            413,
        )
        self.assertEqual((await client.post(url, content="text")).status_code, 415)
        self.assertEqual(len(store.calls), 1)

    async def test_conflict_and_auth_errors_redacted_no_cache(self):
        store = FakeStore()
        client = await self.client(store, lambda request: 42)
        response = await client.post(
            "/partners/api/projects/mine/management/campaigns",
            json={"request_key": str(uuid4()), "title": "conflict"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("no-store", response.headers["cache-control"])

        async def reject(request):
            raise HTTPException(401, "Авторизация устарела.")

        client = await self.client(store, reject)
        self.assertEqual(
            (await client.get("/partners/api/management/projects")).status_code, 401
        )
