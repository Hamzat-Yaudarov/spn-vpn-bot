"""Mocked transport only. No panel credentials, config, DNS or VPN writes."""
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest
from uuid import uuid4

from services.partner_remnawave import (
    PartnerProvisioningError, PartnerRemnawaveProvisioner, PartnerRemnawaveSettings,
)


NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
SECRET = "local-only-remnawave-test-secret"
PROJECT, SUB, REMOTE, SQUAD = map(str, (uuid4(), uuid4(), uuid4(), uuid4()))
SNAPSHOT = {"project_id": PROJECT, "subscription_id": SUB, "remote_username": "wl_" + SUB.replace("-", ""),
            "expires_at": NOW + timedelta(days=30), "traffic_limit_bytes": 300*1024**3,
            "device_limit": 3}


def user(version=2, **changes):
    result = {"username": SNAPSHOT["remote_username"], "description": f"SPN-WL:{PROJECT}:{SUB}",
              "expireAt": SNAPSHOT["expires_at"].isoformat(), "trafficLimitBytes": SNAPSHOT["traffic_limit_bytes"],
              "hwidDeviceLimit": 3, "trafficLimitStrategy": "NO_RESET", "status": "ACTIVE",
              "activeInternalSquads": [{"uuid": SQUAD}], "shortUuid": "TestCredential9",
              "subscriptionUrl": "https://subscriptions.example.test/sub/TestCredential9"}
    result["uuid" if version == 2 else "id"] = REMOTE if version == 2 else 77
    return {**result, **changes}


class Response:
    def __init__(self, body, status=200, raw=None):
        self.status, self.body = status, body
        self.raw = raw if raw is not None else json.dumps(body).encode()
        self.content = self

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def iter_chunked(self, size):
        for start in range(0, len(self.raw), size):
            yield self.raw[start:start+size]


class Factory:
    def __init__(self, responses):
        self.responses, self.requests, self.options = list(responses), [], []

    def __call__(self, **kwargs):
        self.options.append(kwargs)
        return self

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def ok(body): return Response({"response": body})
def absent(): return Response({"errorCode": "A025"}, 404)


class RemnawaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_usage_binds_saved_subscription_credential_to_verified_owned_user(self):
        owned = user(userTraffic={"usedTrafficBytes": 123})
        adapter, transport = self.adapter([ok(owned)])
        snapshot = {**SNAPSHOT, "remote_user_id": REMOTE, "subscription_url": owned["subscriptionUrl"]}
        result = await adapter.read_public_usage(snapshot)
        self.assertEqual(result.used_bytes, 123)
        self.assertEqual([request[0] for request in transport.requests], ["GET"])
        for link in (None, "https://subscriptions.example.test/sub/OtherCredential", "https://evil.test/sub/TestCredential9"):
            adapter, _ = self.adapter([ok(owned)])
            with self.assertRaises(PartnerProvisioningError):
                await adapter.read_public_usage({**snapshot, "subscription_url": link})

    def adapter(self, responses=(), version=2, **changes):
        config = {"api_base_url": "https://panel.example.test/api", "api_token": SECRET,
                  "api_version": version, "internal_squad_uuids": (SQUAD,),
                  "subscription_base_url": "https://subscriptions.example.test", **changes}
        factory = Factory(responses)
        adapter = PartnerRemnawaveProvisioner(PartnerRemnawaveSettings(**config), session_factory=factory, clock=lambda: NOW)
        return adapter, factory

    async def test_constructor_inert_secrets_hidden_and_strict_configuration(self):
        adapter, transport = self.adapter()
        self.assertEqual(transport.requests, [])
        self.assertNotIn(SECRET, repr(adapter))
        self.assertNotIn(SECRET, repr(adapter.settings))
        for changes in ({"api_base_url": "http://panel.example/api"}, {"api_base_url": "https://u:secret@panel.example/api"},
                        {"api_base_url": "https://panel.example/api?token=x"}, {"api_base_url": "https://panel.example/api/other"},
                        {"api_version": True}, {"internal_squad_uuids": ()}, {"internal_squad_uuids": (SQUAD, SQUAD)},
                        {"subscription_base_url": "https://subscriptions.example/unsafe/path"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.adapter(**changes)

    async def test_v2_create_absolute_snapshot_and_verified_readback_no_resets(self):
        adapter, transport = self.adapter([absent(), ok(user()), ok(user())])
        result = await adapter.ensure_subscription(dict(SNAPSHOT))
        self.assertEqual((result.remote_user_id, result.remote_username), (REMOTE, SNAPSHOT["remote_username"]))
        self.assertEqual([r[0] for r in transport.requests], ["GET", "POST", "GET"])
        payload = transport.requests[1][2]["json"]
        self.assertEqual(payload["expireAt"], SNAPSHOT["expires_at"].isoformat())
        self.assertEqual((payload["trafficLimitBytes"], payload["hwidDeviceLimit"], payload["trafficLimitStrategy"]), (300*1024**3, 3, "NO_RESET"))
        self.assertEqual(payload["activeInternalSquads"], [SQUAD])
        self.assertGreaterEqual(len(payload["password"]), 24)
        self.assertNotIn("telegramId", payload)
        for method, url, kwargs in transport.requests:
            self.assertTrue(url.startswith("https://panel.example.test/api/users"))
            self.assertNotIn("reset", url)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + SECRET)
        self.assertTrue(all(not option["trust_env"] and option["timeout"].total == 20 for option in transport.options))

    async def test_v3_existing_user_uses_numeric_id_not_main_uuid_mapping(self):
        adapter, transport = self.adapter([ok(user(3)), ok(user(3)), ok(user(3))], version=3)
        result = await adapter.ensure_subscription({**SNAPSHOT, "remote_user_id": "77"})
        self.assertEqual(result.remote_user_id, "77")
        payload = transport.requests[1][2]["json"]
        self.assertEqual(payload["id"], 77)
        self.assertNotIn("uuid", payload)
        self.assertNotIn("password", payload)

    async def test_lost_create_response_next_attempt_reads_same_name_no_second_create(self):
        adapter, transport = self.adapter([absent(), TimeoutError(SECRET), ok(user()), ok(user()), ok(user())])
        with self.assertRaises(PartnerProvisioningError) as error:
            await adapter.ensure_subscription(dict(SNAPSHOT))
        self.assertNotIn(SECRET, str(error.exception))
        result = await adapter.ensure_subscription(dict(SNAPSHOT))
        self.assertEqual(result.remote_user_id, REMOTE)
        self.assertEqual([r[0] for r in transport.requests], ["GET", "POST", "GET", "PATCH", "GET"])
        self.assertEqual(transport.requests[0][1], transport.requests[2][1])

    async def test_generic404_server_error_redirect_never_prove_absence(self):
        for response in (Response({}, 404), Response({"errorCode": "A025"}, 500), Response({}, 302), TimeoutError(SECRET)):
            adapter, transport = self.adapter([response])
            with self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription(dict(SNAPSHOT))
            self.assertEqual(len(transport.requests), 1)

    async def test_marker_username_and_saved_remote_identity_prevent_collisions(self):
        for changes in ({"description": "foreign project"}, {"username": "tg_100"}, {"uuid": str(uuid4())}):
            adapter, transport = self.adapter([ok(user(**changes))])
            with self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription({**SNAPSHOT, "remote_user_id": REMOTE})
            self.assertEqual(len(transport.requests), 1)
        adapter, transport = self.adapter([absent()])
        with self.assertRaises(PartnerProvisioningError):
            await adapter.ensure_subscription({**SNAPSHOT, "remote_user_id": REMOTE})
        self.assertEqual(len(transport.requests), 1)

    async def test_invalid_snapshots_rejected_before_network(self):
        for changes in ({"project_id": "bad"}, {"remote_username": "tg_100"}, {"expires_at": NOW},
                        {"expires_at": NOW.replace(tzinfo=None)}, {"device_limit": True}, {"device_limit": 11},
                        {"traffic_limit_bytes": 299*1024**3}, {"traffic_limit_bytes": True}):
            adapter, transport = self.adapter()
            with self.subTest(changes=changes), self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription({**SNAPSHOT, **changes})
            self.assertEqual(transport.requests, [])

    async def test_stale_snapshot_cannot_shorten_expiry_quota_or_devices(self):
        for changes in ({"expireAt": (NOW+timedelta(days=31)).isoformat()},
                        {"trafficLimitBytes": SNAPSHOT["traffic_limit_bytes"]+1}, {"hwidDeviceLimit": 4}):
            adapter, transport = self.adapter([ok(user(**changes))])
            with self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription(dict(SNAPSHOT))
            self.assertEqual(len(transport.requests), 1)
        adapter, transport = self.adapter([ok(user(hwidDeviceLimit=4)), ok(user()), ok(user())])
        await adapter.ensure_subscription({**SNAPSHOT, "kind": "device_expiry"})
        self.assertEqual(transport.requests[1][2]["json"]["hwidDeviceLimit"], 3)

    async def test_mutation200_not_enough_exact_readback_required(self):
        for changes in ({"expireAt": (NOW+timedelta(days=29)).isoformat()}, {"trafficLimitBytes": 1},
                        {"hwidDeviceLimit": 4}, {"activeInternalSquads": []}, {"trafficLimitStrategy": "MONTH"},
                        {"status": "DISABLED"}, {"status": "EXPIRED"}, {"uuid": str(uuid4())}):
            adapter, transport = self.adapter([ok(user()), ok(user()), ok(user(**changes))])
            with self.subTest(changes=changes), self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription(dict(SNAPSHOT))
            self.assertEqual(len(transport.requests), 3)

    async def test_subscription_url_bound_to_approved_origin_and_credential(self):
        for url in ("http://subscriptions.example.test/sub/TestCredential9", "https://evil.example/sub/TestCredential9",
                    "https://u:pass@subscriptions.example.test/sub/TestCredential9", "https://subscriptions.example.test:444/sub/TestCredential9",
                    "https://subscriptions.example.test/sub/DifferentCredential", "https://subscriptions.example.test/sub/TestCredential9?token=secret"):
            adapter, _ = self.adapter([ok(user()), ok(user()), ok(user(subscriptionUrl=url))])
            with self.subTest(url=url), self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription(dict(SNAPSHOT))

    async def test_bounded_unambiguous_json_and_response_shape(self):
        for raw in (b'{"response":{},"response":{}}', b"x"*131073, b'[]', b'{"response":[]}'):
            adapter, transport = self.adapter([Response({}, raw=raw)])
            with self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription(dict(SNAPSHOT))
            self.assertEqual(len(transport.requests), 1)

    async def test_v3_rejects_ambiguous_mixed_ids(self):
        for changes in ({"id": True}, {"id": "77"}, {"uuid": REMOTE}, {"id": 0}):
            adapter, transport = self.adapter([ok(user(3, **changes))], version=3)
            with self.assertRaises(PartnerProvisioningError):
                await adapter.ensure_subscription(dict(SNAPSHOT))
            self.assertEqual(len(transport.requests), 1)

    async def test_read_usage_requires_verified_identity_real_counter_and_no_mutations(self):
        adapter,transport=self.adapter([ok(user(userTraffic={"usedTrafficBytes":12345}))])
        result=await adapter.read_usage({**SNAPSHOT,"remote_user_id":REMOTE})
        self.assertEqual((result.project_id,result.subscription_id,result.used_bytes),(PROJECT,SUB,12345))
        self.assertEqual([r[0] for r in transport.requests],["GET"])
        for changes in ({"userTraffic":None},{"userTraffic":{}},{"userTraffic":{"usedTrafficBytes":True}},
                        {"userTraffic":{"usedTrafficBytes":-1}},{"description":"other-project"}):
            adapter,transport=self.adapter([ok(user(**changes))])
            with self.assertRaises(PartnerProvisioningError):
                await adapter.read_usage({**SNAPSHOT,"remote_user_id":REMOTE})
            self.assertEqual([r[0] for r in transport.requests],["GET"])

    async def test_usage_floor_blocks_external_counter_reset_before_patch(self):
        adapter,transport=self.adapter([ok(user(userTraffic={"usedTrafficBytes":0}))])
        with self.assertRaises(PartnerProvisioningError):
            await adapter.ensure_subscription({**SNAPSHOT,"remote_user_id":REMOTE,"usage_floor_bytes":12345})
        self.assertEqual([r[0] for r in transport.requests],["GET"])
