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


if __name__ == "__main__":
    unittest.main()
