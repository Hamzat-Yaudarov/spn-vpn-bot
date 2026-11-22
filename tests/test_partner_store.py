"""Offline contract tests. No config import, environment loading or live database."""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import AsyncMock
from uuid import UUID

import asyncpg

from services.partner_store import (
    InvalidProject, PartnerStore, ProjectConflict, ProjectNotFound, validate_changes,
)


PROJECT_ID = UUID("640874ab-4226-4588-90ed-4a91c37903a4")


def project_row(**overrides):
    row = {
        "id": PROJECT_ID, "owner_tg_id": 42, "slug": "my-vpn", "title": "Мой VPN",
        "price_kopecks": 19900, "version": 1, "status": "draft",
        "support_url": "", "news_url": "", "brand": "{}", "bot_id": None,
        "bot_username": None, "created_at": datetime(2026, 10, 18, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 10, 18, tzinfo=timezone.utc),
        "encrypted_token": "secret-must-never-leak", "future_private_column": "private",
    }
    row.update(overrides)
    return row


class RecordingConnection:
    """Records SQL contract only; deliberately NOT an in-memory database."""
    def __init__(self, rows=(), value=None, many=(), execute_error=None):
        self.rows, self.value, self.many = list(rows), value, many
        self.calls, self.committed, self.rolled_back = [], False, False
        self.execute_error = execute_error

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if self.execute_error and "wl_project_audit" in sql:
            raise self.execute_error

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        response = self.rows.pop(0)
        if isinstance(response, Exception):
            raise response
        return response(sql, args) if callable(response) else copy.deepcopy(response)

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return self.value

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return copy.deepcopy(self.many)

    def acquire(self):
        connection = self

        class Acquire:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, *args):
                return False
        return Acquire()

    def transaction(self):
        connection = self

        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, *args):
                connection.committed = exc_type is None
                connection.rolled_back = exc_type is not None
                return False
        return Transaction()


class PartnerStoreContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_uses_durable_request_and_audit_in_same_transaction(self):
        def inserted(sql, args):
            return project_row(id=args[0])
        conn = RecordingConnection(rows=[None, inserted])
        store = PartnerStore(lambda: conn)
        result = await store.create_project(42, "request-123", "my-vpn", "Мой VPN")
        self.assertTrue(conn.committed)
        self.assertEqual(result["price_kopecks"], 19900)
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["version"], 1)
        self.assertNotIn("encrypted_token", result)
        self.assertNotIn("future_private_column", result)
        request = next(call for call in conn.calls if "INSERT INTO wl_project_create_requests" in call[1])
        self.assertEqual(request[2][0:2], (42, "request-123"))
        self.assertEqual(str(request[2][3]), result["id"])
        self.assertEqual(len(request[2][2]), 64)
        self.assertIn("pg_advisory_xact_lock", conn.calls[0][1])
        audit = next(call for call in conn.calls if "INSERT INTO wl_project_audit" in call[1])
        self.assertEqual(audit[2][1:4], (42, "created", 1))
        self.assertEqual(json.loads(audit[2][4]), {"fields": []})

    async def test_create_retry_returns_existing_updated_project_without_second_insert(self):
        fingerprint = hashlib.sha256(json.dumps({
            "slug": "my-vpn", "title": "Мой VPN", "price_kopecks": 19900,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        conn = RecordingConnection(rows=[{"fingerprint": fingerprint, "project_id": PROJECT_ID}, project_row(version=4)])
        result = await PartnerStore(lambda: conn).create_project(42, "request-123", "my-vpn", "Мой VPN")
        self.assertEqual(result["version"], 4)
        self.assertFalse(any("INSERT" in sql for _, sql, _ in conn.calls))
        project_query = conn.calls[-1]
        self.assertIn("owner_tg_id=$2", project_query[1])
        self.assertEqual(project_query[2], (PROJECT_ID, 42))

    async def test_changed_request_payload_conflicts(self):
        conn = RecordingConnection(rows=[{"fingerprint": "0" * 64, "project_id": PROJECT_ID}])
        with self.assertRaises(ProjectConflict):
            await PartnerStore(lambda: conn).create_project(42, "request-123", "my-vpn", "Other")
        self.assertTrue(conn.rolled_back)
        self.assertFalse(any("INSERT" in sql for _, sql, _ in conn.calls))

    async def test_slug_unique_error_is_safe_conflict(self):
        conn = RecordingConnection(rows=[None, asyncpg.UniqueViolationError("secret-db-detail")])
        with self.assertRaisesRegex(ProjectConflict, "адрес проекта") as caught:
            await PartnerStore(lambda: conn).create_project(42, "request-123", "my-vpn", "Мой VPN")
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(conn.rolled_back)

    async def test_owner_is_enforced_for_list_and_get(self):
        conn = RecordingConnection(rows=[project_row()], many=[project_row()])
        getter = AsyncMock(return_value=conn)
        store = PartnerStore(getter)
        self.assertEqual(len(await store.list_projects(42)), 1)
        result = await store.get_project(str(PROJECT_ID), 42)
        self.assertEqual(result["bot"], {"id": None, "username": None})
        self.assertEqual(result["brand"], {})
        self.assertTrue(all("owner_tg_id=" in sql for _, sql, _ in conn.calls))
        self.assertTrue(all("encrypted_token" not in sql for _, sql, _ in conn.calls))
        self.assertEqual(getter.await_count, 2)

    async def test_foreign_and_nonexistent_get_are_indistinguishable(self):
        for owner in (42, 99):
            conn = RecordingConnection(rows=[None])
            with self.assertRaisesRegex(ProjectNotFound, "Проект не найден"):
                await PartnerStore(lambda: conn).get_project(PROJECT_ID, owner)
            self.assertEqual(conn.calls[0][2], (PROJECT_ID, owner))

    async def test_update_is_parameterized_scoped_and_versioned(self):
        payload = "VPN '); DROP TABLE users; --"
        conn = RecordingConnection(rows=[project_row(title=payload, version=2, brand={"accent_color": "#ffee00"})])
        result = await PartnerStore(lambda: conn).update_project(PROJECT_ID, 42, 1, {
            "title": payload, "price_kopecks": 29950, "brand": {"accent_color": "#FFEE00"},
        })
        sql, args = conn.calls[0][1:]
        self.assertNotIn(payload, sql)
        self.assertIn("id=$1 AND owner_tg_id=$2 AND version=$3", sql)
        self.assertIn("version=version+1", sql)
        self.assertEqual(args[0:3], (PROJECT_ID, 42, 1))
        self.assertEqual(result["version"], 2)
        audit_payload = conn.calls[-1][2][-1]
        self.assertNotIn(payload, audit_payload)
        self.assertEqual(json.loads(audit_payload)["fields"], ["brand", "price_kopecks", "title"])

    async def test_update_distinguishes_own_stale_version_from_not_found(self):
        for exists, exception in ((1, ProjectConflict), (None, ProjectNotFound)):
            conn = RecordingConnection(rows=[None], value=exists)
            with self.assertRaises(exception):
                await PartnerStore(lambda: conn).update_project(PROJECT_ID, 42, 1, {"title": "VPN"})
            self.assertTrue(conn.rolled_back)
            self.assertFalse(any("wl_project_audit" in sql for _, sql, _ in conn.calls))
            self.assertIn("owner_tg_id=$2", conn.calls[-1][1])

    async def test_audit_failure_rolls_back_project_update(self):
        conn = RecordingConnection(rows=[project_row(version=2)], execute_error=RuntimeError("audit unavailable"))
        with self.assertRaises(RuntimeError):
            await PartnerStore(lambda: conn).update_project(PROJECT_ID, 42, 1, {"title": "VPN"})
        self.assertTrue(conn.rolled_back)

    async def test_bot_ciphertext_is_only_written_never_returned_or_audited(self):
        ciphertext = "encrypted:" + "x" * 100
        conn = RecordingConnection(rows=[project_row(bot_id=12345, bot_username="MyProjectBot", version=2)])
        result = await PartnerStore(lambda: conn).save_bot_connection(PROJECT_ID, 42, 1, 12345, "MyProjectBot", ciphertext)
        self.assertEqual(result["bot"], {"id": 12345, "username": "MyProjectBot"})
        self.assertNotIn(ciphertext, str(result))
        self.assertEqual(conn.calls[0][2][-1], ciphertext)
        self.assertNotIn(ciphertext, str(conn.calls[-1]))
        self.assertIn("owner_tg_id=$2 AND version=$3", conn.calls[0][1])

    async def test_bot_uniqueness_conflict_does_not_reveal_another_project(self):
        conn = RecordingConnection(rows=[asyncpg.UniqueViolationError("another-owner:secret")])
        with self.assertRaises(ProjectConflict) as caught:
            await PartnerStore(lambda: conn).save_bot_connection(PROJECT_ID, 42, 1, 12345, "MyProjectBot", "x" * 100)
        self.assertNotIn("another-owner", str(caught.exception))
        self.assertTrue(conn.rolled_back)

    async def test_bot_connection_cannot_update_foreign_or_stale_project(self):
        for exists, error in ((1, ProjectConflict), (None, ProjectNotFound)):
            conn = RecordingConnection(rows=[None], value=exists)
            with self.assertRaises(error):
                await PartnerStore(lambda: conn).save_bot_connection(PROJECT_ID, 42, 1, 12345, "MyProjectBot", "x" * 100)
            self.assertTrue(conn.rolled_back)

    async def test_input_validation_rejects_before_acquiring_pool(self):
        getter = AsyncMock()
        store = PartnerStore(getter)
        invalid = [
            (0, "request-123", "my-vpn", "VPN", 19900),
            (True, "request-123", "my-vpn", "VPN", 19900),
            (42, "tiny", "my-vpn", "VPN", 19900),
            (42, "request-123", "admin", "VPN", 19900),
            (42, "request-123", "xn--my-vpn", "VPN", 19900),
            (42, "request-123", " my-vpn", "VPN", 19900),
            (42, "request-123", "-vpn", "VPN", 19900),
            (42, "request-123", "my-vpn", " ", 19900),
            (42, "request-123", "my-vpn", "VPN\nOther", 19900),
            (42, "request-123", "my-vpn", "VPN", 19899),
            (42, "request-123", "my-vpn", "VPN", 10000001),
            (42, "request-123", "my-vpn", "VPN", 199.0),
            (42, "request-123", "my-vpn", "VPN", True),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(InvalidProject):
                await store.create_project(*args)
        for changes in ({}, {"slug": "other-vpn"}, {"status": "active"}, {"entry_paid": True},
                        {"balance": 999}, {"encrypted_token": "x" * 100}, {"price_kopecks": 19899}):
            with self.subTest(changes=changes), self.assertRaises(InvalidProject):
                await store.update_project(PROJECT_ID, 42, 1, changes)
        getter.assert_not_called()

    async def test_malformed_ids_versions_and_bot_fields_rejected_before_pool(self):
        getter = AsyncMock()
        store = PartnerStore(getter)
        for value in ("not-uuid", None, 42):
            with self.assertRaises(InvalidProject):
                await store.get_project(value, 42)
        for version in (0, True, "1", 2**31):
            with self.assertRaises(InvalidProject):
                await store.update_project(PROJECT_ID, 42, version, {"title": "VPN"})
        for bot_id, username, token in ((0, "MyBot", "x" * 100), (True, "MyBot", "x" * 100),
                                       (1, "bad/name", "x" * 100), (1, "MyBot", "short"),
                                       (1, "MyBot", "12345:" + "x" * 35)):
            with self.assertRaises(InvalidProject):
                await store.save_bot_connection(PROJECT_ID, 42, 1, bot_id, username, token)
        getter.assert_not_called()


class SettingsValidationTests(unittest.TestCase):
    def test_brand_has_no_arbitrary_html_css_javascript_or_remote_code_field(self):
        for brand in ({"html": "<script>"}, {"custom_css": "url(...)"}, {"accent_color": "red"},
                      {"logo_url": "javascript:alert(1)"}, {"welcome_text": "x" * 2001}, [], None):
            with self.subTest(brand=brand), self.assertRaises(InvalidProject):
                validate_changes({"brand": brand})

    def test_links_must_be_https_without_credentials_or_alternate_port(self):
        for url in ("http://t.me/support", "javascript:alert(1)", "https://u:p@example.test/",
                    "https://example.test:444/", "https://example.test/\nfoo", "https://example.test\\foo"):
            with self.subTest(url=url), self.assertRaises(InvalidProject):
                validate_changes({"support_url": url})
        self.assertEqual(validate_changes({"support_url": "https://t.me/help", "news_url": ""}),
                         {"support_url": "https://t.me/help", "news_url": ""})

    def test_schema_only_creates_prefixed_registry_tables(self):
        sql = (Path(__file__).resolve().parents[1] / "ops/partner-platform/schema.sql").read_text()
        self.assertEqual(sql.count("CREATE TABLE IF NOT EXISTS wl_"), 3)
        self.assertNotIn("UPDATE users", sql)
        self.assertNotIn("UPDATE payments", sql)
        self.assertNotIn("DELETE FROM", sql)
        self.assertIn("PRIMARY KEY (owner_tg_id, request_key)", sql)
        self.assertIn("bot_id BIGINT UNIQUE", sql)
        self.assertIn("slug TEXT NOT NULL UNIQUE", sql)
        self.assertIn("REFERENCES wl_projects(id, owner_tg_id)", sql)


if __name__ == "__main__":
    unittest.main()
