"""Pure validation/lock tests. Never import main settings or access a live DB."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest import IsolatedAsyncioTestCase
from uuid import uuid4

from services.partner_commerce_store import (
    PartnerCommerceStore,
    InvalidCommerce,
    _https,
    _now,
    _price,
)


class CommerceValidationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pid = str(uuid4())
        self.calls = 0

        def forbidden_pool():
            self.calls += 1
            raise AssertionError("Validation must happen before DB access")

        self.store = PartnerCommerceStore(forbidden_pool)

    async def test_constructor_and_bad_ids(self):
        with self.assertRaises(TypeError):
            PartnerCommerceStore(None)
        for pid, tg in (("bad", 5), (self.pid, True), (self.pid, 0), (self.pid, "5")):
            with self.assertRaises(InvalidCommerce):
                await self.store.register_customer(pid, tg)
        self.assertEqual(self.calls, 0)

    async def test_money_and_request_key_are_exact(self):
        for amount in (True, 199.0, "19900", 19899, -1, 10000001):
            with self.assertRaises(InvalidCommerce):
                await self.store.create_order(self.pid, 5, str(uuid4()), amount)
        with self.assertRaises(InvalidCommerce):
            await self.store.create_order(self.pid, 5, "not-a-uuid", 19900)
        self.assertEqual(_price(19999), 19999)
        self.assertEqual(self.calls, 0)

    async def test_untrusted_currency_and_naive_time(self):
        for currency in ("USD", "rub", None):
            with self.assertRaises(InvalidCommerce):
                await self.store.record_verified_payment(
                    self.pid, str(uuid4()), "p1", 19900, currency
                )
        with self.assertRaises(InvalidCommerce):
            await self.store.record_verified_payment(
                self.pid, str(uuid4()), "p1", 19900, "RUB", now=datetime(2026, 1, 1)
            )
        self.assertEqual(self.calls, 0)
        self.assertEqual(
            _now(datetime(2026, 1, 1, tzinfo=timezone.utc)).tzinfo, timezone.utc
        )

    async def test_safe_https_only(self):
        for url in (
            "http://example.org",
            "https://user:password@example.org",
            "https://a:444/x",
            "https://a\\b/x",
            "https://a/ x",
            "https://a/\n",
        ):
            with self.assertRaises(InvalidCommerce):
                await self.store.save_invoice(self.pid, 5, str(uuid4()), "p1", url)
        self.assertEqual(
            _https("https://checkout.example.org/a?b=c"),
            "https://checkout.example.org/a?b=c",
        )
        self.assertEqual(self.calls, 0)

    async def test_input_limits_before_db(self):
        for method in (self.store.list_orders, self.store.list_customers):
            with self.assertRaises(InvalidCommerce):
                await method(self.pid, 5, limit=501)
        with self.assertRaises(InvalidCommerce):
            await self.store.complete_activation(
                self.pid,
                str(uuid4()),
                remote_user_id="r",
                subscription_url="javascript:bad",
            )
        with self.assertRaises(InvalidCommerce):
            await self.store.save_invoice(
                self.pid, 5, str(uuid4()), "\n", "https://pay.example.org"
            )
        self.assertEqual(self.calls, 0)

    async def test_activation_lock_uses_session_and_releases_on_error(self):
        calls = []

        class Conn:
            async def fetchval(self, *args):
                return 1

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

        store = PartnerCommerceStore(lambda: Pool())
        with self.assertRaisesRegex(RuntimeError, "remote failure"):
            async with store.acquire_activation_lock(self.pid, str(uuid4())):
                self.assertEqual(len(calls), 1)
                raise RuntimeError("remote failure")
        self.assertIn("pg_advisory_lock", calls[0][0])
        self.assertIn("pg_advisory_unlock", calls[1][0])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[2][0], "released")

    async def test_bounded_pool_session_lease_has_no_nested_acquire_deadlock(self):
        # Emulates native-PG pool exhaustion: each outer lock keeps a connection.
        # A lease must reuse it even when concurrent workers occupy every slot.
        for capacity in (1, 2):
            semaphore = asyncio.Semaphore(capacity)
            session_lock = asyncio.Lock()
            acquired = []
            pid, sid, oid = uuid4(), uuid4(), uuid4()

            class Conn:
                async def fetchval(self, *args):
                    return 1

                async def fetchrow(self, *args):
                    return {
                        "id": oid,
                        "project_id": pid,
                        "tg_id": 5,
                        "subscription_id": sid,
                        "status": "active",
                    }

                async def execute(self, sql, *args):
                    if "pg_advisory_unlock" in sql:
                        session_lock.release()
                    elif "pg_advisory_lock" in sql:
                        await session_lock.acquire()

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

            store = PartnerCommerceStore(lambda: Pool())
            leases = []

            async def worker():
                async with store.acquire_activation_lock(pid, sid) as locked:
                    leases.append(locked)
                    await asyncio.sleep(0.01)
                    self.assertEqual(
                        (await locked.get_order(pid, 5, oid))["id"], str(oid)
                    )

            await asyncio.wait_for(asyncio.gather(worker(), worker()), timeout=1)
            self.assertEqual(
                len(acquired), 2, "each worker acquires exactly one native connection"
            )
            self.assertFalse(session_lock.locked())
            with self.assertRaisesRegex(RuntimeError, "lease has already ended"):
                await leases[0].get_order(pid, 5, oid)

    async def test_catalog_loader_can_read_the_same_single_connection_pool(self):
        pid, sid = uuid4(), uuid4()
        now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        sub = {
            "id": sid,
            "project_id": pid,
            "tg_id": 5,
            "expires_at": now + timedelta(days=30),
            "desired_expires_at": now + timedelta(days=30),
            "device_limit": 3,
            "traffic_limit_bytes": 300 * 1024**3,
            "last_activation_sequence": 1,
        }
        semaphore = asyncio.Semaphore(1)

        class Conn:
            async def fetchrow(self, sql, *args):
                if "FROM wl_projects" in sql:
                    return {
                        "id": pid,
                        "status": "active",
                        "price_kopecks": 19900,
                        "bot_id": 123456,
                    }
                if "FROM wl_customers" in sql:
                    return {"tg_id": 5}
                if "FROM wl_subscriptions" in sql:
                    return dict(sub)
                if "INSERT INTO wl_orders" in sql:
                    return {"id": args[0], "price_kopecks": args[6], "purpose": args[7]}
                return None

            async def fetchval(self, sql, *args):
                if "FROM wl_customers" in sql:
                    return 1
                return 0 if "sum(device_count)" in sql else None

            @asynccontextmanager
            async def transaction(self):
                yield

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                await semaphore.acquire()
                try:
                    yield Conn()
                finally:
                    semaphore.release()

        pool = Pool()

        async def loader(subscription, *, now):
            async with pool.acquire():
                return {
                    "traffic_status": "available",
                    "traffic_packages": [
                        {"id": "gb_10", "gb": 10, "price_kopecks": 2400}
                    ],
                }

        store = PartnerCommerceStore(lambda: pool, loader, entitlements_enabled=True)
        catalogue = await asyncio.wait_for(
            store.get_addon_catalog(pid, 5, sid, now=now), timeout=1
        )
        self.assertEqual(catalogue["traffic_packages"][0]["price_kopecks"], 2400)
        order = await asyncio.wait_for(
            store.create_addon_order(
                pid,
                5,
                uuid4(),
                2400,
                subscription_id=sid,
                purpose="traffic",
                package_id="gb_10",
                now=now,
            ),
            timeout=1,
        )
        self.assertEqual(order["purpose"], "traffic")
