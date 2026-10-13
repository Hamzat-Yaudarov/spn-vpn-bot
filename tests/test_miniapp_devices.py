"""MiniApp device availability uses existing prices, without live services."""
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

with patch('dotenv.load_dotenv', return_value=False), patch.dict(os.environ, {'DATABASE_URL': '', 'ADMIN_ID': '999'}):
    import webhooks
    from services.device_addons import available_device_addon_packages, miniapp_device_addon_options


NOW = datetime(2026, 10, 13, 12)


def subscription(**changes):
    return {
        'id': 7, 'tg_id': 123, 'generation': 'v2', 'is_visible': True,
        'is_renewable': True, 'plan_kind': 'regular', 'type_index': 1,
        'subscription_until': NOW + timedelta(days=30), 'hwid_device_limit': 5,
        'remnawave_uuid': None, **changes,
    }


class DeviceAvailabilityTests(unittest.TestCase):
    def test_available_options_reuse_exact_existing_prices_and_period(self):
        for kind, limit in [('regular', 5), ('bypass', 3), ('regular', 7), ('regular', 8), ('bypass', 9)]:
            with self.subTest(kind=kind, limit=limit):
                sub = subscription(plan_kind=kind, hwid_device_limit=limit)
                result = miniapp_device_addon_options(sub, now=NOW)
                self.assertEqual(result['addon_status'], 'available')
                self.assertEqual(result['packages'], available_device_addon_packages(sub, now=NOW))
                self.assertEqual([p['count'] for p in result['packages']], list(range(1, min(3, 10-limit)+1)))

    def test_expired_missing_and_boundary_deadline_do_not_offer_payment(self):
        for until in [None, NOW, NOW-timedelta(seconds=1)]:
            with self.subTest(until=until):
                self.assertEqual(miniapp_device_addon_options(subscription(subscription_until=until), now=NOW),
                                 {'addon_status': 'expired', 'packages': []})

    def test_maximum_is_distinct_from_ineligibility(self):
        self.assertEqual(miniapp_device_addon_options(subscription(hwid_device_limit=10), now=NOW),
                         {'addon_status': 'max_limit', 'packages': []})
        for changes in [{'is_renewable': False}, {'is_visible': False}, {'generation': 'legacy'}]:
            with self.subTest(changes=changes):
                self.assertEqual(miniapp_device_addon_options(subscription(**changes), now=NOW),
                                 {'addon_status': 'unavailable', 'packages': []})
        self.assertEqual(miniapp_device_addon_options(None, now=NOW)['addon_status'], 'unavailable')

    def test_aware_dates_are_normalized_without_changing_billable_days(self):
        utc = NOW.replace(tzinfo=timezone.utc)
        local = timezone(timedelta(hours=4))
        expected = miniapp_device_addon_options(subscription(), now=NOW)
        actual = miniapp_device_addon_options(subscription(subscription_until=(utc+timedelta(days=30)).astimezone(local)), now=utc)
        self.assertEqual(actual, expected)


class DeviceAPIConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_response_reports_authoritative_status_and_addons(self):
        until = datetime.utcnow() + timedelta(days=30)
        for renewable, expected in [(True, 'available'), (False, 'unavailable')]:
            with self.subTest(renewable=renewable), patch.object(webhooks.db, 'get_active_device_addon_count', new=AsyncMock(return_value=2)):
                result = await webhooks._serialize_subscription(subscription(subscription_until=until, is_renewable=renewable))
            self.assertEqual(result['devices']['limit'], 7)
            self.assertEqual(result['devices']['max_limit'], 10)
            self.assertEqual(result['devices']['addon_status'], expected)
            self.assertEqual(bool(result['devices']['packages']), renewable)

    async def test_unavailable_and_expired_reject_before_creating_invoice(self):
        until = datetime.utcnow() + timedelta(days=30)
        for changes, addon_count in [({'is_renewable':False}, 0), ({'subscription_until':datetime.utcnow()-timedelta(days=1)}, 0), ({}, 5)]:
            sub = subscription(subscription_until=until, **changes) if 'subscription_until' not in changes else subscription(**changes)
            request = SimpleNamespace(json=AsyncMock(return_value={'subscription_id':7, 'device_count':1, 'provider':'yookassa'}))
            with (
                self.subTest(changes=changes, addons=addon_count),
                patch.object(webhooks, '_miniapp_user', new=AsyncMock(return_value={'id':123})),
                patch.object(webhooks.db, 'get_subscription_by_id', new=AsyncMock(return_value=sub)),
                patch.object(webhooks.db, 'get_active_device_addon_count', new=AsyncMock(return_value=addon_count)),
                patch.object(webhooks, 'create_yookassa_payment', new=AsyncMock()) as provider,
                patch.object(webhooks.db, 'create_device_addon_purchase', new=AsyncMock()) as purchase,
                patch.object(webhooks, '_bot', object()),
            ):
                with self.assertRaises(webhooks.HTTPException) as error:
                    await webhooks.miniapp_create_device_payment(request)
                self.assertEqual(error.exception.status_code, 400)
                provider.assert_not_awaited()
                purchase.assert_not_awaited()

    async def test_device_serializer_keeps_ua_for_name_but_does_not_change_identifiers(self):
        result = webhooks._serialize_device({'hwid':'local-device', 'platform':'Android', 'userAgent':'INCY/1.0', 'createdAt':'2026-08-30T12:00:00Z'})
        self.assertEqual(result['hwid'], 'local-device')
        self.assertEqual(result['user_agent'], 'INCY/1.0')
        self.assertEqual(result['created_at'], '2026-08-30T12:00:00Z')


if __name__ == '__main__':
    unittest.main()
