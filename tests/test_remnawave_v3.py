import json
import unittest
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from services import remnawave as api, remnawave_identity as identity


LOCAL = "f30a18ee-68d3-4919-8b78-8615391bc94b"
OTHER = "8684556a-02e7-4747-bd24-cb078c957beb"
USER = {"id": 1234, "username": "tg_test", "status": "ACTIVE",
        "expireAt": "2030-01-01T00:00:00Z", "shortUuid": "abcdefghijklmnop"}


class Response:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = {"response": dict(USER)} if body is None else body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.body

    async def text(self):
        return json.dumps(self.body)


class HTTP:
    def __init__(self, responses=None):
        self.responses = deque(responses or [])
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.popleft() if self.responses else Response()

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(identity, "REMNAWAVE_API_VERSION", 3))

    async def test_missing_mapping_fails_closed(self):
        with patch.object(identity.db, "db_execute", AsyncMock(return_value=None)):
            with self.assertRaises(identity.IdentityError):
                await identity.api_user_id(LOCAL)

    async def test_id_lookup_is_panel_scoped(self):
        with patch.object(identity.db, "db_execute", AsyncMock(return_value={"user_id": 1234})) as query:
            self.assertEqual(await identity.api_user_id(LOCAL), 1234)
        self.assertEqual(query.call_args.args[1], (identity.PANEL_URL, uuid.UUID(LOCAL)))

    def test_numeric_id_validation(self):
        for value in (None, "1234", True, 0, -1, 2**53):
            with self.subTest(value=value), self.assertRaises(identity.IdentityError):
                identity.numeric_user_id({"id": value})

    async def test_v2_still_uses_uuid_and_records_id(self):
        query = AsyncMock(side_effect=[None, {"local_uuid": uuid.UUID(LOCAL)}])
        with patch.object(identity, "REMNAWAVE_API_VERSION", 2), patch.object(identity.db, "db_execute", query):
            self.assertEqual(await identity.api_user_id(LOCAL), LOCAL)
            self.assertEqual(await identity.remember_remote_user(dict(USER, uuid=LOCAL)), LOCAL)
        self.assertEqual(query.call_args.args[1][1:3], (uuid.UUID(LOCAL), 1234))

    async def test_existing_v3_user_preserves_old_uuid(self):
        with patch.object(identity.db, "db_execute", AsyncMock(return_value={"local_uuid": uuid.UUID(LOCAL)})):
            self.assertEqual(await identity.remember_remote_user(USER), LOCAL)

    async def test_new_v3_user_has_stable_local_uuid_not_vpn_credential(self):
        inserted = []

        async def query(sql, params, **kwargs):
            if "UNION" in sql:
                return []
            if "INSERT" in sql:
                inserted.append(params)
                return {"local_uuid": params[1]}
            return None

        with patch.object(identity.db, "db_execute", side_effect=query):
            first = await identity.remember_remote_user(dict(USER, vlessUuid=OTHER))
            second = await identity.remember_remote_user(dict(USER, vlessUuid=OTHER))
        self.assertEqual(first, second)
        self.assertNotEqual(first, OTHER)
        self.assertEqual(uuid.UUID(first).version, 5)
        self.assertEqual(inserted[0][2], 1234)

    async def test_unmapped_v3_user_uses_exact_existing_username_reference(self):
        query = AsyncMock(side_effect=[None, [{"remnawave_uuid": uuid.UUID(LOCAL)}],
                                      {"local_uuid": uuid.UUID(LOCAL)}])
        with patch.object(identity.db, "db_execute", query):
            self.assertEqual(await identity.remember_remote_user(USER), LOCAL)

    async def test_ambiguous_username_or_conflict_is_rejected(self):
        for responses in ([None, [{"remnawave_uuid": LOCAL}, {"remnawave_uuid": OTHER}]],
                          [None, [], None]):
            with patch.object(identity.db, "db_execute", AsyncMock(side_effect=responses)):
                with self.assertRaises(identity.IdentityError):
                    await identity.remember_remote_user(USER)

    async def test_api_version_mismatch_is_rejected(self):
        with self.assertRaises(identity.IdentityError):
            await identity.remember_remote_user(dict(USER, uuid=LOCAL))
        with patch.object(identity, "REMNAWAVE_API_VERSION", 2), self.assertRaises(identity.IdentityError):
            await identity.remember_remote_user(USER)

    def test_past_expiry_disables_v3_and_future_does_not_override_traffic_status(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(identity.expiry_fields(past), {"status": "DISABLED"})
        self.assertEqual(identity.expiry_fields(past.replace(tzinfo=None)), {"status": "DISABLED"})
        future = past + timedelta(days=30)
        self.assertEqual(identity.expiry_fields(future), {"expireAt": future.isoformat()})
        with patch.object(identity, "REMNAWAVE_API_VERSION", 2):
            self.assertEqual(identity.expiry_fields(past), {"expireAt": past.isoformat()})

    async def test_disabled_expiry_overlay_is_used_only_for_matching_disabled_user(self):
        past = datetime(2026, 1, 1)
        with patch.object(identity.db, "db_execute", AsyncMock(return_value={"user_id": 1234, "disabled_expire_at": past})):
            data = await identity.normalize_user_info(LOCAL, dict(USER, status="DISABLED"))
            self.assertEqual(data["uuid"], LOCAL)
            self.assertEqual(data["expireAt"], "2026-01-01T00:00:00Z")
            active = await identity.normalize_user_info(LOCAL, USER)
            self.assertEqual(active["expireAt"], USER["expireAt"])
            with self.assertRaises(identity.IdentityError):
                await identity.normalize_user_info(LOCAL, dict(USER, id=999))


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(identity, "REMNAWAVE_API_VERSION", 3))
        self.resolve = self.enterContext(patch.object(identity, "api_user_id", AsyncMock(return_value=1234)))
        self.expiry = self.enterContext(patch.object(identity, "remember_expiry", AsyncMock()))
        self.reactivate = self.enterContext(patch.object(identity, "should_reactivate", AsyncMock(return_value=False)))
        self.enterContext(patch.object(api, "_verified_connector", return_value=None))
        self.enterContext(patch("utils.asyncio.sleep", AsyncMock()))

    async def test_update_uses_numeric_id_and_keeps_limits(self):
        http = HTTP()
        with patch.object(api.aiohttp, "ClientSession", http):
            success = await api.remnawave_update_user_profile(
                None, LOCAL, traffic_limit_bytes=210 * 1024**3,
                traffic_limit_strategy="NO_RESET", hwid_device_limit=7,
                active_internal_squads=[OTHER], telegram_id=123,
            )
        self.assertTrue(success)
        body = http.calls[0][2]["json"]
        self.assertNotIn("uuid", body)
        self.assertEqual(body["id"], 1234)
        self.assertEqual(body["trafficLimitBytes"], 210 * 1024**3)
        self.assertEqual(body["hwidDeviceLimit"], 7)
        self.assertEqual(body["activeInternalSquads"], [OTHER])

    async def test_past_expiry_uses_disabled_and_records_overlay(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        http = HTTP()
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertTrue(await api.remnawave_set_subscription_expiry(None, LOCAL, past))
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][0], "POST")
        self.assertTrue(http.calls[0][1].endswith("/users/1234/actions/disable"))
        self.expiry.assert_awaited_once_with(LOCAL, past)

    async def test_disabled_action_is_repeatable(self):
        http = HTTP([Response(400, {"errorCode": "A029"})])
        past = datetime.now(timezone.utc) - timedelta(days=1)
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertTrue(await api.remnawave_set_subscription_expiry(None, LOCAL, past))
        self.expiry.assert_awaited_once_with(LOCAL, past)

    async def test_other_disable_errors_do_not_claim_success(self):
        http = HTTP([Response(400, {"errorCode": "OTHER"}) for _ in range(3)])
        past = datetime.now(timezone.utc) - timedelta(days=1)
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertFalse(await api.remnawave_set_subscription_expiry(None, LOCAL, past))
        self.expiry.assert_not_awaited()

    async def test_expired_profile_disabled_before_changing_traffic(self):
        http = HTTP()
        past = datetime.now(timezone.utc) - timedelta(days=1)
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertTrue(await api.remnawave_update_user_profile(None, LOCAL, expire_at=past, traffic_limit_bytes=0))
        self.assertEqual([call[0] for call in http.calls], ["POST", "PATCH"])
        self.assertEqual(http.calls[1][2]["json"], {"id": 1234, "status": "DISABLED", "trafficLimitBytes": 0})

    async def test_future_date_reactivates_only_bot_disabled_profile(self):
        future = datetime.now(timezone.utc) + timedelta(days=30)
        for reactivate in (True, False):
            self.reactivate.return_value = reactivate
            http = HTTP([Response(body={"response": dict(USER, status="DISABLED")})] if reactivate else [])
            with patch.object(api.aiohttp, "ClientSession", http):
                self.assertTrue(await api.remnawave_set_subscription_expiry(None, LOCAL, future))
            payload = http.calls[-1][2]["json"]
            self.assertEqual(payload.get("status"), "ACTIVE" if reactivate else None)
            self.assertEqual(payload["expireAt"], future.isoformat())

    async def test_stale_disabled_marker_cannot_reactivate_limited_profile(self):
        self.reactivate.return_value = True
        http = HTTP([Response(body={"response": dict(USER, status="LIMITED")})])
        future = datetime.now(timezone.utc) + timedelta(days=30)
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertTrue(await api.remnawave_set_subscription_expiry(None, LOCAL, future))
        self.assertNotIn("status", http.calls[-1][2]["json"])

    async def test_v2_profile_still_uses_uuid_and_past_expiration(self):
        self.resolve.return_value = LOCAL
        past = datetime.now(timezone.utc) - timedelta(days=1)
        http = HTTP()
        with patch.object(identity, "REMNAWAVE_API_VERSION", 2), patch.object(api.aiohttp, "ClientSession", http):
            self.assertTrue(await api.remnawave_set_subscription_expiry(None, LOCAL, past))
        self.assertEqual(http.calls[0][0], "PATCH")
        self.assertEqual(http.calls[0][2]["json"], {"uuid": LOCAL, "expireAt": past.isoformat()})

    async def test_reset_delete_and_hwid_requests_use_numeric_id(self):
        http = HTTP([Response(), Response(204), Response(body={"response": {"devices": []}}), Response(), Response()])
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertTrue(await api.remnawave_reset_user_traffic(None, LOCAL))
            self.assertTrue(await api.remnawave_delete_user(None, LOCAL))
            self.assertEqual(await api.remnawave_get_hwid_devices(None, LOCAL), [])
            self.assertTrue(await api.remnawave_delete_hwid_device(None, LOCAL, "device-test"))
            self.assertTrue(await api.remnawave_delete_all_hwid_devices(None, LOCAL))
        paths = [call[1].removeprefix(api.REMNAWAVE_BASE_URL) for call in http.calls]
        self.assertEqual(paths, ["/users/1234/actions/reset-traffic", "/users/1234", "/hwid/devices/1234",
                                 "/hwid/devices/delete", "/hwid/devices/delete-all"])
        self.assertEqual(http.calls[3][2]["json"], {"userId": 1234, "hwid": "device-test"})
        self.assertEqual(http.calls[4][2]["json"], {"userId": 1234})

    async def test_revoke_uses_documented_v3_endpoint(self):
        http = HTTP()
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertTrue(await api.remnawave_revoke_subscription(None, LOCAL))
        self.assertTrue(http.calls[0][1].endswith("/users/1234/actions/revoke"))

    async def test_add_to_squad_never_calls_all_users_endpoint(self):
        http = HTTP([Response(202)])
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertTrue(await api.remnawave_add_to_squad(None, LOCAL, OTHER))
        self.assertTrue(http.calls[0][1].endswith("/bulk-actions/add-many-users"))
        self.assertEqual(http.calls[0][2]["json"], {"userIds": [1234]})

    async def test_v2_squad_update_keeps_existing_squads(self):
        with (patch.object(identity, "REMNAWAVE_API_VERSION", 2),
              patch.object(api, "remnawave_get_user_info", AsyncMock(return_value={"activeInternalSquads": [{"uuid": LOCAL}]})),
              patch.object(api, "remnawave_update_user_profile", AsyncMock(return_value=True)) as update):
            self.assertTrue(await api.remnawave_add_to_squad(None, LOCAL, OTHER))
        update.assert_awaited_once_with(None, LOCAL, active_internal_squads=[LOCAL, OTHER])

    async def test_missing_mapping_sends_no_remote_requests(self):
        self.resolve.side_effect = identity.IdentityError("not mapped")
        http = HTTP()
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertFalse(await api.remnawave_delete_user(None, LOCAL))
            self.assertFalse(await api.remnawave_update_user_profile(None, LOCAL, missing_user_is_success=True))
        self.assertEqual(http.calls, [])

    async def test_lookup_error_does_not_create_user(self):
        for status, body in ((503, {}), (404, {"message": "wrong endpoint"})):
            http = HTTP([Response(status, body) for _ in range(4)])
            with patch.object(api.aiohttp, "ClientSession", http):
                self.assertEqual(await api.remnawave_get_or_create_user(None, 123), (None, None))
            self.assertTrue(all(call[0] == "GET" for call in http.calls))

    async def test_missing_404_code_requires_complete_listing_before_create(self):
        http = HTTP([Response(404, {"message": "Not Found"}),
                     Response(body={"response": {"users": [], "total": 0}}), Response()])
        with (patch.object(api.aiohttp, "ClientSession", http),
              patch.object(identity, "remember_remote_user", AsyncMock(return_value=LOCAL))):
            self.assertEqual(await api.remnawave_get_or_create_user(None, 123, remna_username="tg_test"), (LOCAL, "tg_test"))
        self.assertEqual([c[0] for c in http.calls], ["GET", "GET", "POST"])
        self.assertTrue(http.calls[1][1].endswith("/users"))

    async def test_lookup_fallback_preserves_existing_user_on_later_page(self):
        http = HTTP([Response(404, {}),
                     Response(body={"response": {"users": [dict(USER, id=9, username="a")], "total": 2}}),
                     Response(body={"response": {"users": [USER], "total": 2}})])
        with (patch.object(api.aiohttp, "ClientSession", http),
              patch.object(identity, "remember_remote_user", AsyncMock(return_value=LOCAL))):
            self.assertEqual(await api.remnawave_get_or_create_user(None, 123, remna_username="tg_test"), (LOCAL, "tg_test"))
        self.assertEqual([c[0] for c in http.calls], ["GET", "GET", "GET"])
        self.assertEqual(http.calls[2][2]["params"]["start"], 1)

    async def test_fallback_never_creates_from_incomplete_or_invalid_listing(self):
        for bad in ({"response": {"users": [], "total": 2}}, {"response": {"users": [], "total": False}},
                    {"response": {"users": [{"username": "other"}], "total": 1}}, {}):
            http = HTTP([Response(404, {}), Response(body=bad)] * 2)
            with patch.object(api.aiohttp, "ClientSession", http):
                self.assertEqual(await api.remnawave_get_or_create_user(None, 123), (None, None))
            self.assertTrue(all(c[0] == "GET" for c in http.calls))

    async def test_lookup_refuses_other_username_and_auth_errors(self):
        for response in (Response(), Response(401, {}), Response(403, {})):
            http = HTTP([response, response])
            with patch.object(api.aiohttp, "ClientSession", http):
                self.assertEqual(await api.remnawave_get_or_create_user(None, 123, remna_username="another"), (None, None))
            self.assertEqual([c[0] for c in http.calls], ["GET", "GET"])

    async def test_create_v3_user_only_after_confirmed_not_found(self):
        http = HTTP([Response(404, {"errorCode": "A025"}), Response()])
        with (patch.object(api.aiohttp, "ClientSession", http),
              patch.object(identity, "remember_remote_user", AsyncMock(return_value=LOCAL))):
            result = await api.remnawave_get_or_create_user(None, 123, remna_username="tg_test")
        self.assertEqual(result, (LOCAL, "tg_test"))
        self.assertEqual([call[0] for call in http.calls], ["GET", "POST"])
        self.assertNotIn("password", http.calls[1][2]["json"])

    async def test_delete_requires_actual_user_not_found_error(self):
        http = HTTP([Response(404, {"errorCode": "A025"})])
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertTrue(await api.remnawave_delete_user(None, LOCAL))
        http = HTTP([Response(404, {}) for _ in range(3)])
        with patch.object(api.aiohttp, "ClientSession", http):
            self.assertFalse(await api.remnawave_delete_user(None, LOCAL))

    async def test_extension_of_expired_subscription_starts_now(self):
        before = datetime.now(timezone.utc)
        with (patch.object(api, "remnawave_get_user_info", AsyncMock(return_value={"expireAt": "2020-01-01T00:00:00Z"})),
              patch.object(api, "remnawave_set_subscription_expiry", AsyncMock(return_value=True)) as update):
            self.assertTrue(await api.remnawave_extend_subscription(None, LOCAL, 30))
        expiry = update.call_args.args[2]
        self.assertGreaterEqual(expiry, before + timedelta(days=30))
        self.assertLessEqual(expiry, datetime.now(timezone.utc) + timedelta(days=30))

    async def test_subscription_url_uses_numeric_id_without_rotating_key(self):
        http = HTTP([Response(body={"response": dict(USER, subscriptionUrl="https://old.example/same-short-id")})])
        with patch.object(api.aiohttp, "ClientSession", http):
            url = await api.remnawave_get_subscription_url(None, LOCAL)
        self.assertTrue(url.endswith("/same-short-id"))
        self.assertTrue(http.calls[0][1].endswith("/users/1234"))
        self.assertEqual(http.calls[0][0], "GET")

    async def test_short_uuid_auth_preserves_local_alias(self):
        http = HTTP()
        with (patch.object(api.aiohttp, "ClientSession", http),
              patch.object(identity, "remember_remote_user", AsyncMock(return_value=LOCAL))):
            self.assertEqual(await api.remnawave_resolve_user_uuid_by_short_uuid(USER["shortUuid"]), LOCAL)


if __name__ == "__main__":
    unittest.main()
