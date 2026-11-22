from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import database as db
from handlers import admin


class BroadcastFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_inactive_broadcast_requires_valid_days_and_current_preview(self):
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=2, user_id=2))
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=2), chat=SimpleNamespace(id=2), message_id=10,
            answer=AsyncMock(return_value=SimpleNamespace(message_id=11)), text="-1",
        )
        callback = SimpleNamespace(
            from_user=message.from_user, message=SimpleNamespace(message_id=11, edit_text=AsyncMock()),
            bot=SimpleNamespace(copy_message=AsyncMock()), answer=AsyncMock(),
        )
        recipients = [{"tg_id": 3}]
        with (patch.object(admin, "ADMIN_ID", 2),
              patch.object(admin, "_get_broadcast_users", AsyncMock(return_value=recipients)) as get_users,
              patch.object(admin, "_run_broadcast_copy", AsyncMock()) as broadcast):
            await admin.admin_broadcast(message, state, SimpleNamespace(command="not_sub_sms"))
            await admin.handle_broadcast_inactive_days(message, state)
            self.assertEqual(await state.get_state(), admin.BroadcastStates.waiting_for_inactive_days.state)

            message.text = "15"
            await admin.handle_broadcast_inactive_days(message, state)
            message.text = "Специальное предложение"
            await admin.handle_broadcast_message(message, state)
            self.assertIn("от 15 дн. включительно", message.answer.await_args.args[0])
            await admin.start_selected_broadcast(callback, state)
            get_users.assert_not_awaited()

            await admin.preview_broadcast(callback, state)
            callback.message.message_id = 9
            await admin.start_selected_broadcast(callback, state)
            get_users.assert_not_awaited()

            callback.message.message_id = 11
            await admin.start_selected_broadcast(callback, state)
            await admin.start_selected_broadcast(callback, state)
            get_users.assert_awaited_once_with("no_sub", 15)
            broadcast.assert_awaited_once()
            self.assertEqual(broadcast.await_args.args[2], recipients)


@unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional isolated PostgreSQL/WASM environment")
class BroadcastAudienceSQLTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_with_latest_expiry_registration_and_deleted_subscription(self):
        from test_payment_recovery_postgres import Bridge

        bridge = await Bridge().start()
        self.addAsyncCleanup(bridge.close)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = now - timedelta(days=15)
        before = cutoff - timedelta(seconds=1)
        after = cutoff + timedelta(seconds=1)
        for tg_id in (-1, 1, 2, 3, 4, 5, 6):
            await bridge.execute(
                "INSERT INTO users (tg_id, created_at) VALUES ($1, $2)",
                tg_id, after if tg_id == 5 else before,
            )
        for tg_id, slot, expires in [(1, 1, cutoff), (2, 1, after), (3, 1, before),
                                     (3, 2, now + timedelta(days=1)), (6, 1, now + timedelta(days=1))]:
            subscription_id = await bridge.fetchval(
                "INSERT INTO subscriptions (tg_id, slot_number, subscription_until) VALUES ($1, $2, $3) RETURNING id",
                tg_id, slot, expires,
            )
        await bridge.execute("CREATE TABLE IF NOT EXISTS notification_state (subscription_id BIGINT)")

        with (patch.object(db, "get_pool", AsyncMock(return_value=bridge)),
              patch.object(admin, "_broadcast_inactive_cutoff", return_value=cutoff)):
            self.assertTrue(await db.delete_subscription_record(subscription_id))
            for audience, days, expected in (("all", None, [1, 2, 3, 4, 5, 6]),
                                             ("active", None, [3]), ("no_sub", 15, [1, 4])):
                users = await admin._get_broadcast_users(audience, days)
                self.assertEqual([user["tg_id"] for user in users], expected)
