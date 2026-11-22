"""Offline finance validation, auth/API boundaries and authenticated encryption."""

import json
import unittest
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
import httpx

from partner_finance_web import create_router
from services.partner_finance import (
    FinanceConflict,
    FinanceForbidden,
    FinanceUnavailable,
    FinanceVault,
    InvalidFinance,
    PartnerFinanceStore,
    proportional_reversal,
    validate_requisites,
)


class FinanceValidationTests(unittest.TestCase):
    def test_sbp_normalized_and_unknown_fields_rejected(self):
        self.assertEqual(
            validate_requisites("sbp", {"bank": "Банк", "phone": "8 (999) 123-45-67"}),
            {"bank": "Банк", "phone": "+79991234567"},
        )
        for value in (
            {"bank": "x", "phone": "+79991234567", "paid": True},
            {"bank": "x", "phone": "+19991234567"},
            {"bank": "x", "phone": "no"},
            {"bank": "x\nlink", "phone": "+79991234567"},
        ):
            with self.assertRaises(InvalidFinance):
                validate_requisites("sbp", value)

    def test_usdt_only_address_no_automatic_network_or_rate(self):
        address = "T" + "a" * 33
        self.assertEqual(
            validate_requisites("usdt", {"address": address}), {"address": address}
        )
        for value in (
            {"address": "https://site.test/address"},
            {"address": address, "rate": 90},
            {"address": "tiny"},
        ):
            with self.assertRaises(InvalidFinance):
                validate_requisites("usdt", value)

    def test_partial_reversal_rounding_complete_original_share_exactly(self):
        amount, partner, used, returned = 19901, 13931, 0, 0
        for increment in [1, 1, 3, 7, 31, 300, 1000, 18558]:
            p, platform = proportional_reversal(
                amount, partner, used, returned, increment
            )
            self.assertEqual(p + platform, increment)
            used += increment
            returned += p
        self.assertEqual((used, returned), (amount, partner))
        with self.assertRaises(InvalidFinance):
            proportional_reversal(19900, 13930, 19900, 13930, 1)
        with self.assertRaises(FinanceConflict):
            proportional_reversal(19900, 13930, 10000, 1, 10)
        with self.assertRaises(InvalidFinance):
            proportional_reversal(True, 0, 0, 0, 1)

    def test_vault_bound_identity_purpose_key_rotation_and_no_repr_secret(self):
        first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        vault = FinanceVault([first])
        project, identity = str(uuid4()), str(uuid4())
        value = {"phone": "+79991234567", "bank": "Банк"}
        encrypted = vault.encrypt(project, identity, "requisites", value)
        self.assertNotIn(value["phone"], encrypted)
        self.assertNotIn(first, repr(vault))
        rotated = FinanceVault([second, first])
        self.assertEqual(
            rotated.decrypt(project, identity, "requisites", encrypted), value
        )
        self.assertIn(vault.fingerprints(value)[0], rotated.fingerprints(value))
        for pid, wid, purpose in (
            (str(uuid4()), identity, "requisites"),
            (project, str(uuid4()), "requisites"),
            (project, identity, "transfer"),
        ):
            with self.assertRaises(FinanceUnavailable):
                rotated.decrypt(pid, wid, purpose, encrypted)
        with self.assertRaises(FinanceUnavailable):
            FinanceVault([])

    def test_service_disabled_and_operator_ids_explicit(self):
        store = PartnerFinanceStore(lambda: None)
        with self.assertRaises(FinanceUnavailable):
            store.require_operator(1)
        store = PartnerFinanceStore(lambda: None, operator_ids=(99,), enabled=True)
        self.assertEqual(store.require_operator(99), 99)
        with self.assertRaises(FinanceForbidden):
            store.require_operator(42)


class FakeStore:
    def __init__(self):
        self.calls = []
        self.error = None

    def require_operator(self, actor):
        if actor != 99:
            raise FinanceForbidden()

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.error:
                raise self.error
            return {"status": "pending", "amount_kopecks": 150000}

        return call


class FinanceHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeStore()

        async def authenticate(request):
            # Test stand-in; production callback verifies manager Telegram initData.
            if request.headers.get("x-test-auth") is None:
                raise HTTPException(401, "No manager auth")
            return int(request.headers["x-test-auth"])

        self.app = FastAPI()
        self.app.include_router(
            create_router(self.store, authenticate=authenticate, enabled=True)
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://manager.test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.pid = str(uuid4())
        self.base = f"/partners/api/projects/{self.pid}/finance"
        self.operator = (
            f"/partners/api/operator/projects/{self.pid}/withdrawals/{uuid4()}"
        )
        self.body = {
            "request_key": str(uuid4()),
            "amount_kopecks": 150000,
            "method": "sbp",
            "requisites": {"bank": "Банк", "phone": "+79991234567"},
        }

    async def test_auth_before_mutation_and_operator_not_partner(self):
        result = await self.client.post(self.base + "/withdrawals", json=self.body)
        self.assertEqual(result.status_code, 401)
        result = await self.client.post(
            self.operator + "/paid",
            headers={"x-test-auth": "42"},
            json={
                "actual_amount_kopecks": 150000,
                "transfer_reference": "bank:transfer-1",
                "confirmed_transferred": True,
            },
        )
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.store.calls, [])

    async def test_owner_identity_never_from_body_and_cached_details_forbidden(self):
        result = await self.client.post(
            self.base + "/withdrawals", headers={"x-test-auth": "42"}, json=self.body
        )
        self.assertEqual(result.status_code, 200)
        self.assertIn("no-store", result.headers["cache-control"])
        self.assertEqual(self.store.calls[0][1][1], 42)
        result = await self.client.post(
            self.base + "/withdrawals",
            headers={"x-test-auth": "42"},
            json=self.body | {"owner_id": 99},
        )
        self.assertEqual(result.status_code, 400)

    async def test_bounded_json_duplicates_and_fields(self):
        header = {"x-test-auth": "42", "content-type": "application/json"}
        for content, status in (
            (b"[]", 400),
            (b'{"request_key":"a","request_key":"b"}', 400),
            (b'{"x":"' + b"a" * 9000 + b'"}', 413),
            (b'{"status":"paid"}', 400),
        ):
            result = await self.client.post(
                self.base + "/withdrawals", headers=header, content=content
            )
            self.assertEqual(result.status_code, status)
        self.assertEqual(self.store.calls, [])

    async def test_paid_recording_requires_explicit_already_transferred(self):
        header = {"x-test-auth": "99"}
        payload = {
            "actual_amount_kopecks": 150000,
            "transfer_reference": "bank:transfer-1",
            "confirmed_transferred": False,
        }
        result = await self.client.post(
            self.operator + "/paid", headers=header, json=payload
        )
        self.assertEqual(result.status_code, 400)
        result = await self.client.post(
            self.operator + "/paid",
            headers=header,
            json=payload | {"confirmed_transferred": True},
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.store.calls[0][0], "confirm_withdrawal_paid")

    async def test_internal_errors_redacted(self):
        self.store.error = RuntimeError("SQL leaked +79991234567 cipher secret")
        result = await self.client.get(
            self.base + "/summary", headers={"x-test-auth": "42"}
        )
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("7999", result.text)
        self.assertNotIn("secret", result.text)

    async def test_factory_disabled_does_not_authenticate(self):
        async def forbidden(_):
            raise AssertionError("disabled router must not authenticate")

        app = FastAPI()
        app.include_router(create_router(self.store, authenticate=forbidden))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://manager.test"
        ) as client:
            result = await client.get(self.base + "/summary")
        self.assertEqual(result.status_code, 404)
