import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from services import subscription_deletion as deletion


class ExpiryTests(unittest.TestCase):
    def test_expiry_boundary_naive_and_timezone_aware(self):
        now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
        for until, expired in (
            (None, False),
            (now - timedelta(seconds=1), True),
            (now, True),
            (now + timedelta(seconds=1), False),
            (now.replace(tzinfo=None), True),
            (now.astimezone(timezone(timedelta(hours=4))), True),
        ):
            with self.subTest(until=until):
                self.assertEqual(deletion.subscription_has_expired({'subscription_until':until}, now=now), expired)


class ExpiredOnlyDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_renewed_subscription_is_rechecked_under_user_lock(self):
        old = {'id':7, 'tg_id':123, 'subscription_until':datetime.now(timezone.utc) - timedelta(days=1), 'remnawave_uuid':'test-uuid'}
        renewed = {**old, 'subscription_until':datetime.now(timezone.utc) + timedelta(days=30)}
        with (
            patch.object(deletion.db, 'get_subscription_by_id', new=AsyncMock(side_effect=[old, renewed])) as get,
            patch.object(deletion.db, 'acquire_user_lock', new=AsyncMock(return_value=True)) as lock,
            patch.object(deletion.db, 'release_user_lock', new_callable=AsyncMock) as release,
            patch.object(deletion.db, 'delete_subscription_record', new_callable=AsyncMock) as delete,
            patch.object(deletion, 'remnawave_delete_user', new_callable=AsyncMock) as remote,
        ):
            with self.assertRaises(deletion.SubscriptionActiveError):
                await deletion.delete_subscription_everywhere(7, tg_id=123, actor='user_bot', require_expired=True)
        self.assertEqual(get.await_count, 2)
        lock.assert_awaited_once_with(123)
        release.assert_awaited_once_with(123)
        delete.assert_not_awaited()
        remote.assert_not_awaited()

    async def test_expired_subscription_can_be_deleted_and_admin_policy_is_unchanged(self):
        for actor, require_expired, days in (('user_bot', True, -1), ('web_admin', False, 30)):
            with self.subTest(actor=actor):
                record = {'id':7, 'tg_id':123, 'subscription_until':datetime.now(timezone.utc) + timedelta(days=days), 'remnawave_uuid':'test-uuid'}
                with (
                    patch.object(deletion.db, 'get_subscription_by_id', new=AsyncMock(return_value=record)),
                    patch.object(deletion.db, 'acquire_user_lock', new=AsyncMock(return_value=True)),
                    patch.object(deletion.db, 'release_user_lock', new_callable=AsyncMock) as release,
                    patch.object(deletion.db, 'delete_subscription_record', new=AsyncMock(return_value=True)) as delete,
                    patch.object(deletion, 'remnawave_delete_user', new=AsyncMock(return_value=True)) as remote,
                ):
                    result = await deletion.delete_subscription_everywhere(7, tg_id=123, actor=actor, require_expired=require_expired)
                self.assertEqual(result['subscription'], record)
                remote.assert_awaited_once_with(None, 'test-uuid')
                delete.assert_awaited_once_with(7)
                release.assert_awaited_once_with(123)

    async def test_unknown_expiry_cannot_be_deleted_by_user(self):
        record = {'id':7, 'tg_id':123, 'subscription_until':None}
        with (
            patch.object(deletion.db, 'get_subscription_by_id', new=AsyncMock(return_value=record)),
            patch.object(deletion.db, 'acquire_user_lock', new=AsyncMock(return_value=True)),
            patch.object(deletion.db, 'release_user_lock', new_callable=AsyncMock) as release,
            patch.object(deletion.db, 'delete_subscription_record', new_callable=AsyncMock) as delete,
        ):
            with self.assertRaises(deletion.SubscriptionActiveError):
                await deletion.delete_subscription_everywhere(7, tg_id=123, require_expired=True)
        delete.assert_not_awaited()
        release.assert_awaited_once_with(123)


if __name__ == '__main__':
    unittest.main()
