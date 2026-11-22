"""Pure enrollment validation and connection-lease tests; no settings/network."""
import asyncio
from datetime import datetime, timezone
from unittest import IsolatedAsyncioTestCase
from uuid import uuid4

from services.partner_enrollment_store import (
    PartnerEnrollmentStore, InvalidEnrollment, _https, _now, _public,
)


class EnrollmentValidationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pid, self.oid = str(uuid4()), str(uuid4())
        self.calls = 0

        def forbidden_pool():
            self.calls += 1
            raise AssertionError("Validation must occur before database access")
        self.store = PartnerEnrollmentStore(forbidden_pool)

    async def test_bad_identifiers_and_constructor(self):
        with self.assertRaises(TypeError):
            PartnerEnrollmentStore(None)
        for pid, owner in (("bad", 42), (self.pid, True), (self.pid, 0), (self.pid, "42")):
            with self.assertRaises(InvalidEnrollment):
                await self.store.get_current_entry_order(pid, owner)
        for key in ("not-uuid", True, 1):
            with self.assertRaises(InvalidEnrollment):
                await self.store.create_entry_order(self.pid, 42, key)
        self.assertEqual(self.calls, 0)

    async def test_positive_integer_fee_and_currency_before_database(self):
        # Positive integer prices/amounts are compared with the saved row and
        # server policy inside the transaction, not a global 9990-only parser.
        for fee in (True, 999000.0, "999000", 0, -1, 2**63):
            with self.assertRaises(InvalidEnrollment):
                await self.store.create_entry_order(self.pid, 42, str(uuid4()), fee)
            with self.assertRaises(InvalidEnrollment):
                await self.store.record_verified_payment(self.pid, self.oid, "p", fee, "RUB")
        for currency in ("USD", "rub", "", None):
            with self.assertRaises(InvalidEnrollment):
                await self.store.record_verified_payment(self.pid, self.oid, "p", 999000, currency)
        self.assertEqual(self.calls, 0)

    async def test_https_invoice_and_webhook_rules(self):
        for url in ("http://pay.example.org", "https://user:pass@pay.example.org", "https://a:444/x",
                    "https://a\\b/x", "https://a/ x", "https://a/\n", "javascript:alert(1)"):
            with self.assertRaises(InvalidEnrollment):
                await self.store.save_invoice(self.pid, 42, self.oid, "p1", url)
        self.assertEqual(_https("https://pay.example.org/checkout?a=b"), "https://pay.example.org/checkout?a=b")
        self.assertEqual(_https("https://bots.example.org/hooks/project", webhook=True), "https://bots.example.org/hooks/project")
        for suffix in ("?", "?secret=value", "#", "#fragment"):
            with self.assertRaises(InvalidEnrollment):
                await self.store.complete_launch(self.pid, self.oid, bot_id=1234,
                    webhook_url="https://bots.example.org/hook" + suffix, secret_fingerprint="a" * 64)
        self.assertEqual(self.calls, 0)

    async def test_launch_parameters_are_not_plain_secrets(self):
        for fingerprint in (None, True, "plaintext-secret", "A" * 64, "a" * 63, "a" * 65):
            with self.assertRaises(InvalidEnrollment):
                await self.store.complete_launch(self.pid, self.oid, bot_id=1234,
                    webhook_url="https://bots.example.org/hook", secret_fingerprint=fingerprint)
        with self.assertRaises(InvalidEnrollment):
            await self.store.complete_launch(self.pid, self.oid, bot_id=True,
                webhook_url="https://bots.example.org/hook", secret_fingerprint="a" * 64)
        with self.assertRaises(InvalidEnrollment):
            await self.store.mark_launch_retry(self.pid, self.oid, needs_webhook_replace="true")
        self.assertEqual(self.calls, 0)

    async def test_invalid_provider_and_limits(self):
        for payment in ("", " p1", "p1\n", "x" * 201, None, True):
            with self.assertRaises(InvalidEnrollment):
                await self.store.save_invoice(self.pid, 42, self.oid, payment, "")
        for method in (self.store.list_recoverable_entry_orders, self.store.list_recoverable_launches):
            for limit in (True, 0, 101, "50"):
                with self.assertRaises(InvalidEnrollment):
                    await method(limit)
        self.assertEqual(self.calls, 0)

    async def test_aware_time_only(self):
        with self.assertRaises(InvalidEnrollment):
            await self.store.record_verified_payment(self.pid, self.oid, "p", 999000, "RUB", now=datetime(2026, 1, 1))
        with self.assertRaises(InvalidEnrollment):
            await self.store.mark_launch_retry(self.pid, self.oid, now="now")
        self.assertEqual(_now(datetime(2026, 1, 1, tzinfo=timezone.utc)).tzinfo, timezone.utc)
        self.assertEqual(self.calls, 0)

    async def test_explicit_public_allowlist_never_leaks_private_snapshot(self):
        value = _public({"id": uuid4(), "project_id": uuid4(), "owner_tg_id": 42,
            "encrypted_token": "private-ciphertext", "token": "private-token", "secret_fingerprint": "a" * 64,
            "webhook_url": "https://private.example/hook", "bot_username": "private_extra"})
        self.assertEqual(value["tg_id"], 42)
        self.assertFalse(value["needs_webhook_replace"])
        self.assertNotIn("private", str(value))
        self.assertNotIn("secret_fingerprint", value)

    async def test_launch_lock_unlocks_on_error(self):
        calls = []

        class Conn:
            async def fetchrow(self, *args):
                return {"id": self_pid, "owner_tg_id": 42, "bot_id": 1234, "status": "draft"}

            async def execute(self, sql, key):
                calls.append((sql, key))

        class Acquire:
            async def __aenter__(self):
                return Conn()

            async def __aexit__(self, *args):
                calls.append(("released", None))

        class Pool:
            def acquire(self):
                return Acquire()

        self_pid = self.pid
        store = PartnerEnrollmentStore(lambda: Pool())
        with self.assertRaisesRegex(RuntimeError, "remote error"):
            async with store.acquire_launch_lock(self.pid):
                raise RuntimeError("remote error")
        self.assertIn("pg_advisory_lock", calls[0][0])
        self.assertIn("pg_advisory_unlock", calls[1][0])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[2][0], "released")

    async def test_bounded_pool_one_and_two_have_no_nested_checkout_deadlock(self):
        for capacity in (1, 2):
            semaphore, advisory = asyncio.Semaphore(capacity), asyncio.Lock()
            acquired, leases = [], []
            pid, oid = uuid4(), uuid4()

            class Conn:
                async def fetchrow(self, sql, *args):
                    if "wl_entry_orders" in sql:
                        return {"id": oid, "project_id": pid, "owner_tg_id": 42, "status": "paid_pending_launch"}
                    return {"id": pid, "owner_tg_id": 42, "bot_id": 1234, "status": "draft"}

                async def fetchval(self, *args):
                    return False

                async def execute(self, sql, *args):
                    if "pg_advisory_unlock" in sql:
                        advisory.release()
                    elif "pg_advisory_lock" in sql:
                        await advisory.acquire()

            class Acquire:
                async def __aenter__(self):
                    await semaphore.acquire()
                    acquired.append(1)
                    return Conn()

                async def __aexit__(self, *args):
                    semaphore.release()

            class Pool:
                def acquire(self):
                    return Acquire()

            store = PartnerEnrollmentStore(lambda: Pool())

            async def worker():
                async with store.acquire_launch_lock(pid) as locked:
                    leases.append(locked)
                    await asyncio.sleep(0.01)
                    self.assertEqual((await locked.get_entry_order(pid, 42, oid))["id"], str(oid))

            await asyncio.wait_for(asyncio.gather(worker(), worker()), 1)
            self.assertEqual(len(acquired), 2)
            self.assertFalse(advisory.locked())
            with self.assertRaisesRegex(RuntimeError, "lease has already ended"):
                await leases[0].get_entry_order(pid, 42, oid)
