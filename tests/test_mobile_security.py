import base64
import hashlib
import hmac
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import mobile_api
from fastapi import HTTPException
from services import cryptobot, mobile_auth, remnawave


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ChallengeConnection:
    def __init__(self, row):
        self.row = row
        self.sessions = []

    def transaction(self):
        return _AsyncContext()

    async def fetchrow(self, query, *params):
        if "mobile_auth_challenges" in query:
            return self.row
        return None

    async def execute(self, query, *params):
        if "INSERT INTO mobile_sessions" in query:
            self.sessions.append(params)
        if "UPDATE mobile_auth_challenges" in query:
            self.row["status"] = "consumed"
            self.row["consumed_at"] = datetime.utcnow()


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


class _AccessKeyConnection:
    def __init__(self, tg_id=1001):
        self.tg_id = tg_id
        self.sessions = []

    def transaction(self):
        return _AsyncContext()

    async def fetchrow(self, query, *params):
        if "FROM mobile_access_keys" in query:
            return {"tg_id": self.tg_id}
        return None

    async def execute(self, query, *params):
        if "INSERT INTO mobile_sessions" in query:
            self.sessions.append(params)


class MobileAuthPrimitiveTests(unittest.IsolatedAsyncioTestCase):
    def test_pkce_challenge_is_urlsafe_sha256(self):
        verifier = "A" * 64
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(mobile_auth.code_challenge_for_verifier(verifier), expected)

    @patch("services.mobile_auth.db.db_execute", new_callable=AsyncMock)
    async def test_challenge_stores_only_start_token_hash(self, execute):
        result = await mobile_auth.create_challenge("B" * 43, "Pixel")
        params = execute.await_args.args[1]
        self.assertNotEqual(params[1], result["start_token"])
        self.assertEqual(params[1], mobile_auth.hash_secret(result["start_token"]))

    async def test_challenge_is_consumed_once(self):
        verifier = "C" * 64
        row = {
            "code_challenge": mobile_auth.code_challenge_for_verifier(verifier),
            "consumed_at": None,
            "expires_at": datetime.utcnow() + timedelta(minutes=2),
            "status": "approved",
            "approved_tg_id": 1001,
            "device_name": "Pixel",
        }
        connection = _ChallengeConnection(row)
        with patch("services.mobile_auth.db.get_pool", new=AsyncMock(return_value=_Pool(connection))):
            tokens = await mobile_auth.exchange_challenge(str(uuid.uuid4()), verifier)
            self.assertIn("access_token", tokens)
            self.assertEqual(len(connection.sessions), 1)
            with self.assertRaises(mobile_auth.MobileAuthError) as second:
                await mobile_auth.exchange_challenge(str(uuid.uuid4()), verifier)
        self.assertEqual(second.exception.code, "invalid_challenge")

    async def test_wrong_verifier_is_rejected(self):
        row = {
            "code_challenge": mobile_auth.code_challenge_for_verifier("D" * 64),
            "consumed_at": None,
            "expires_at": datetime.utcnow() + timedelta(minutes=2),
            "status": "approved",
            "approved_tg_id": 1001,
            "device_name": "Pixel",
        }
        with patch("services.mobile_auth.db.get_pool", new=AsyncMock(return_value=_Pool(_ChallengeConnection(row)))):
            with self.assertRaises(mobile_auth.MobileAuthError) as error:
                await mobile_auth.exchange_challenge(str(uuid.uuid4()), "E" * 64)
        self.assertEqual(error.exception.code, "invalid_verifier")

    def test_access_key_supports_compact_and_legacy_formats(self):
        access_key = mobile_auth.generate_access_key()
        self.assertRegex(access_key, mobile_auth.ACCESS_KEY_RE)
        self.assertEqual(mobile_auth.normalize_access_key(f"  {access_key} \n"), access_key)
        legacy = "WAY-ABCD-EFGH-JKLM-NPQR-STUV-WXYZ"
        self.assertEqual(mobile_auth.normalize_access_key(legacy.lower()), legacy)
        self.assertNotRegex("WAY-ABCD-EFGH-JKLM-NPQR-STUV-WXY0", mobile_auth.ACCESS_KEY_RE)

    @patch("services.mobile_auth.db.db_execute", new_callable=AsyncMock)
    async def test_access_key_is_stored_only_as_hash(self, execute):
        access_key = await mobile_auth.issue_access_key(1001)
        params = execute.await_args.args[1]
        self.assertNotEqual(params[2], access_key)
        self.assertEqual(params[2], mobile_auth.hash_secret(access_key))

    async def test_access_key_creates_separate_device_session(self):
        connection = _AccessKeyConnection()
        access_key = mobile_auth.generate_access_key()
        with patch("services.mobile_auth.db.get_pool", new=AsyncMock(return_value=_Pool(connection))):
            tokens = await mobile_auth.exchange_access_key(access_key, "Pixel 10")
        self.assertIn("access_token", tokens)
        self.assertEqual(len(connection.sessions), 1)
        self.assertEqual(connection.sessions[0][1], 1001)

    async def test_invalid_access_key_is_rejected_without_database_lookup(self):
        with self.assertRaises(mobile_auth.MobileAuthError) as error:
            await mobile_auth.exchange_access_key("WAY-TOO-SHORT")
        self.assertEqual(error.exception.code, "invalid_access_key")


class MobileProfileSecurityTests(unittest.TestCase):
    def test_subscription_proxy_rejects_ssrf_hosts_and_http(self):
        self.assertEqual(
            remnawave.validate_public_subscription_url("https://sub.wayspn.online/sub/abc"),
            "https://sub.wayspn.online/sub/abc",
        )
        for value in (
            "http://sub.wayspn.online/sub/abc",
            "https://evil.example/sub/abc",
            "https://sub.wayspn.online.evil.example/sub/abc",
            "https://sub.wayspn.online/admin",
        ):
            with self.assertRaises(ValueError):
                remnawave.validate_public_subscription_url(value)

    def test_only_supported_protocols_survive_profile_filter(self):
        source = b"vless://one\nvmess://blocked\ntrojan://two\nss://three\nhttp://blocked\n"
        filtered = mobile_api._filter_profile(source).decode()
        self.assertIn("vless://one", filtered)
        self.assertIn("trojan://two", filtered)
        self.assertIn("ss://three", filtered)
        self.assertNotIn("vmess://", filtered)
        self.assertNotIn("http://", filtered)

    def test_base64_subscription_remains_base64(self):
        encoded = base64.b64encode(b"vless://one\nvmess://blocked\n")
        decoded = base64.b64decode(mobile_api._filter_profile(encoded)).decode()
        self.assertEqual(decoded, "vless://one\n")


class CryptoWebhookSignatureTests(unittest.TestCase):
    def test_valid_and_forged_signatures(self):
        raw = b'{"update_type":"invoice_paid","payload":{"invoice_id":1}}'
        token = "test-crypto-token"
        secret = hashlib.sha256(token.encode()).digest()
        signature = hmac.new(secret, raw, hashlib.sha256).hexdigest()
        with patch.object(cryptobot, "CRYPTOBOT_TOKEN", token):
            self.assertTrue(cryptobot.verify_cryptobot_webhook_signature(raw, signature))
            self.assertFalse(cryptobot.verify_cryptobot_webhook_signature(raw + b" ", signature))
            self.assertFalse(cryptobot.verify_cryptobot_webhook_signature(raw, "0" * 64))


class PublicReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_manifest_is_no_store_and_matches_release(self):
        response = await mobile_api.android_update_manifest()
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn(mobile_api.ANDROID_APK_SHA256.encode(), response.body)
        self.assertIn(mobile_api.ANDROID_SIGNING_CERT_SHA256.encode(), response.body)

    async def test_downloads_use_fixed_allowlist(self):
        response = await mobile_api.android_release_artifact("WayVPN-1.0.0-universal-release.apk")
        self.assertIn("immutable", response.headers["cache-control"])
        with self.assertRaises(HTTPException) as rejected:
            await mobile_api.android_release_artifact("../.env")
        self.assertEqual(rejected.exception.status_code, 404)

    async def test_auth_return_is_no_store_and_opens_android_package(self):
        response = await mobile_api.mobile_auth_return()
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn(mobile_api.ANDROID_PACKAGE_ID.encode(), response.body)
        self.assertIn(b"wayvpn://auth-return", response.body)


if __name__ == "__main__":
    unittest.main()
