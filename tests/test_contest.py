"""Contest regressions. Set PGLITE_MODULE to run isolated PostgreSQL SQL tests."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from services import contest


class ContestDrawTests(unittest.TestCase):
    def test_draws_from_all_entrants_and_leaves_ineligible_places_unawarded(self):
        deadline = datetime(2026, 1, 1)
        eligible_expiry = deadline + timedelta(days=45)
        entries = [
            {"tg_id": 1, "subscription_until": eligible_expiry, "paid_invites": 0},
            {"tg_id": 2, "subscription_until": eligible_expiry, "paid_invites": 1},
            {"tg_id": 3, "subscription_until": eligible_expiry - timedelta(seconds=1), "paid_invites": 1},
        ]
        with patch.object(contest, "SystemRandom") as random:
            random.return_value.sample.return_value = entries
            paid_invites, places = contest._select_places(entries, deadline)
        random.return_value.sample.assert_called_once_with(entries, 3)
        self.assertEqual(paid_invites, 2)
        self.assertEqual(len(places), 10)
        self.assertEqual([place.prize.amount for place in places], [20000, 10000, 5000, 365, 180, 90, 30, 30, 30, 30])
        self.assertEqual([place.tg_id for place in places[:3]], [1, 2, 3])
        self.assertEqual([place.status for place in places], ["forfeited", "won", "forfeited"] + ["unfilled"] * 7)
        self.assertEqual(places[0].reasons, ("no_paid_invite",))
        self.assertEqual(places[2].reasons, ("subscription_short",))


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM module is not configured")
class ContestPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_partner_store_postgres import RegistryBridge

        class ContestBridge(RegistryBridge):
            async def execute(self, sql, *params):
                if sql == contest.SCHEMA:
                    await self.query(sql, exec=True)
                else:
                    await super().execute(sql, *params)

            async def query(self, sql, *params, exec=False):
                rows = await super().query(sql, *params, exec=exec)
                if not exec:
                    for row in rows:
                        if row.get("subscription_until"):
                            row["subscription_until"] = row["subscription_until"].replace(tzinfo=None)
                return rows

            async def executemany(self, sql, args):
                for params in args:
                    await self.execute(sql, *params)

        with patch.dict(os.environ, {"TZ": "UTC"}):
            self.sql = await ContestBridge().start()
        self.addAsyncCleanup(self.sql.close)
        await self.sql.query((Path(__file__).resolve().parents[1] / "schema.sql").read_text(), exec=True)
        self.enterContext(patch.object(contest.db, "_pool", self.sql))
        await contest.initialize(self.sql)
        await contest.initialize(self.sql)

    async def test_publication_and_idempotent_enrollment_are_bound_by_schedule(self):
        start = datetime.now(timezone.utc) + timedelta(days=1)
        end = start + timedelta(days=7)
        await self.sql.execute("INSERT INTO users (tg_id) VALUES (123)")
        await contest.create_campaign("draft", "Черновик", start, end)
        self.assertEqual((await contest.enroll("draft", 123)).status, "not_found")
        self.assertIsNone(await contest.get_current_campaign())
        with self.assertRaisesRegex(ValueError, "ссылку"):
            await contest.publish_campaign("draft")
        await contest.create_campaign("open", "Конкурс", start, end, channel_chat_id="@wayspn",
                                      rules_url="https://example.com/contest")
        with self.assertRaisesRegex(ValueError, "числовой ID"):
            await contest.publish_campaign("open")
        campaign = await contest.publish_campaign("open", resolved_channel_chat_id=-1001234567)
        self.assertEqual(campaign["channel_chat_id"], "-1001234567")
        self.assertEqual(await contest.publish_campaign("open"), campaign)
        self.assertEqual((await contest.enroll("open", 123)).status, "closed")
        await self.sql.execute("""
            UPDATE contests SET published_at = now() - INTERVAL '2 days', starts_at = now() - INTERVAL '1 day'
            WHERE slug = 'open'
        """)
        self.assertEqual((await contest.get_current_campaign())["slug"], "open")
        self.assertEqual(await contest.enroll("open", 345), contest.Enrollment("unknown_user", 0))
        self.assertEqual(await contest.enroll("open", 123), contest.Enrollment("joined", 1))
        self.assertEqual(await contest.enroll("open", 123), contest.Enrollment("already_joined", 1))
        self.assertEqual(await contest.count_participants("open"), 1)
        with self.assertRaisesRegex(ValueError, "после окончания"):
            await contest.draw("open")
        self.assertIsNone(await contest.get_draw_result("open"))

    async def test_cutoff_excludes_late_purchases_preserves_deleted_subscriptions_and_freezes_results(self):
        await self.sql.execute("""
            INSERT INTO users (tg_id, referrer_id, created_at) VALUES
                (101, NULL, '2025-01-01'), (102, NULL, '2025-01-01'), (103, NULL, '2025-01-01'),
                (104, NULL, '2025-01-01'), (105, NULL, '2025-01-01'), (106, NULL, '2025-01-01'),
                (201, 101, '2026-01-02'), (202, 102, '2026-01-02'), (203, 103, '2026-01-02'),
                (204, 104, '2025-12-31'), (205, 105, '2026-01-02'), (206, 106, '2026-01-02');
        """)
        await self.sql.execute("""
            INSERT INTO subscriptions (id, tg_id, slot_number, remnawave_uuid, generation, is_visible, subscription_until)
            SELECT tg_id, tg_id, 1, 'c309f867-e098-4cf6-8671-3b4bf95f12c0', 'v2', TRUE,
                   TIMESTAMP '2026-01-10' + (CASE WHEN tg_id = 101 THEN 44 ELSE 45 END) * INTERVAL '1 day'
            FROM users WHERE tg_id BETWEEN 101 AND 106 AND tg_id <> 103
        """)
        await self.sql.execute("""
            INSERT INTO contests (slug, title, starts_at, ends_at, published_at, rules_url, channel_chat_id)
            VALUES ('closed', 'Конкурс', '2026-01-01', '2026-01-10', '2025-12-31', 'https://example.com/contest', '@wayspn')
        """)
        await self.sql.execute("""
            INSERT INTO contest_participants (contest_slug, tg_id, joined_at)
            SELECT 'closed', tg_id, '2026-01-03' FROM users WHERE tg_id BETWEEN 101 AND 106
        """)
        await self.sql.execute("UPDATE subscriptions SET subscription_until = '2026-05-01' WHERE id = 101")
        await self.sql.execute("UPDATE subscriptions SET subscription_until = '2026-05-02' WHERE id = 101")
        await self.sql.execute("DELETE FROM subscriptions WHERE id = 102")
        await self.sql.execute("""
            INSERT INTO subscriptions (id, tg_id, slot_number, remnawave_uuid, generation, is_visible, subscription_until)
            VALUES (103, 103, 1, 'c309f867-e098-4cf6-8671-3b4bf95f12c0', 'v2', TRUE, '2026-05-01')
        """)
        await self.sql.execute("""
            INSERT INTO payments (tg_id, tariff_code, amount, provider, invoice_id, status)
            SELECT tg_id, 'regular_1m', 100, 'yookassa', tg_id::text, 'paid'
            FROM users WHERE tg_id BETWEEN 201 AND 206
        """)
        await self.sql.execute("""
            INSERT INTO referral_payment_settlements (payment_id, invoice_id, payer_id, referrer_id, amount,
                                                      qualified, is_first_purchase, outcome, created_at)
            SELECT payment.id, payment.invoice_id, friend.tg_id, friend.referrer_id, payment.amount,
                   TRUE, TRUE, 'credited', CASE WHEN friend.tg_id = 205 THEN TIMESTAMP '2026-01-10'
                                               ELSE TIMESTAMP '2026-01-04' END
            FROM payments AS payment JOIN users AS friend ON payment.tg_id = friend.tg_id
        """)
        await self.sql.execute("""
            INSERT INTO referral_confirmed_refunds (payment_id, invoice_id, amount, reference, confirmed_by, note, created_at)
            SELECT id, invoice_id, 100, 'refund-test', 999, '', '2026-01-09' FROM payments WHERE tg_id = 206
        """)

        result = await contest.draw("closed")
        self.assertEqual((result.participant_count, result.paid_invites), (6, 3))
        self.assertEqual(await contest.enroll("closed", 201), contest.Enrollment("closed", 6))
        winners = {place.tg_id: place for place in result.places if place.tg_id}
        self.assertEqual(winners[101].reasons, ("subscription_short",))
        self.assertEqual(winners[102].status, "won")
        self.assertEqual(winners[103].reasons, ("subscription_short",))
        for tg_id in (104, 105, 106):
            self.assertEqual(winners[tg_id].reasons, ("no_paid_invite",))
        self.assertEqual([place.status for place in result.places[6:]], ["unfilled"] * 4)

        await self.sql.execute("UPDATE subscriptions SET subscription_until = '2027-01-01' WHERE id = 101")
        self.assertEqual(await contest.draw("closed"), result)
        self.assertEqual(await contest.get_draw_result("closed"), result)
