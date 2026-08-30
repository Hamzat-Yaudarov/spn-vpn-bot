import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from scripts import prepare_remnawave_v3 as prep
from test_remnawave_v3 import HTTP, Response, LOCAL, OTHER, USER


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    def test_export_contains_only_identity_fields(self):
        data = dict(USER, uuid=LOCAL, trojanPassword="secret", vlessUuid=OTHER,
                    subscriptionUrl="https://secret.example/key", telegramId=123)
        result = prep.public_identity(data, 2)
        self.assertEqual(result, {"local_uuid": LOCAL, "user_id": 1234, "username": "tg_test"})

    def test_wrong_panel_version_stops_preparation(self):
        with self.assertRaises(prep.PreparationError):
            prep.public_identity(USER, 2)
        with self.assertRaises(prep.PreparationError):
            prep.public_identity(dict(USER, uuid=LOCAL), 3)

    async def test_pagination_is_complete_and_read_only(self):
        http = HTTP([Response(body={"response": {"users": [dict(USER, uuid=LOCAL)], "total": 2}}),
                     Response(body={"response": {"users": [dict(USER, uuid=OTHER, id=1235, username="other")], "total": 2}})])
        data = await prep.fetch_identities(http, "https://example.test/api", 2)
        self.assertEqual(len(data), 2)
        self.assertEqual(http.calls[1][2]["params"]["start"], 1)
        self.assertTrue(all(call[0] == "GET" for call in http.calls))
        self.assertFalse(http.calls[0][2]["allow_redirects"])

    async def test_empty_panel_is_valid(self):
        http = HTTP([Response(body={"response": {"users": [], "total": 0}})])
        self.assertEqual(await prep.fetch_identities(http, "https://example.test/api", 2), [])

    async def test_incomplete_or_duplicate_export_is_rejected(self):
        for last in ([], [dict(USER, uuid=LOCAL)]):
            http = HTTP([Response(body={"response": {"users": [dict(USER, uuid=LOCAL)], "total": 2}}),
                         Response(body={"response": {"users": last, "total": 2}})])
            with self.assertRaises(prep.PreparationError):
                await prep.fetch_identities(http, "https://example.test/api", 2)

    async def test_changed_total_stops_export(self):
        http = HTTP([Response(body={"response": {"users": [dict(USER, uuid=LOCAL)], "total": 2}}),
                     Response(body={"response": {"users": [], "total": 3}})])
        with self.assertRaises(prep.PreparationError):
            await prep.fetch_identities(http, "https://example.test/api", 2)

    def test_conflicting_maps_are_never_overwritten(self):
        candidate = prep.public_identity(dict(USER, uuid=LOCAL), 2)
        for old in (dict(candidate, user_id=999), dict(candidate, local_uuid=OTHER)):
            with self.assertRaises(prep.PreparationError):
                prep.validate_mapping_conflicts([candidate], [old])
        prep.validate_mapping_conflicts([candidate], [candidate])

    def test_coverage_counts_missing_active_separately(self):
        refs = [{"local_uuid": uuid.UUID(LOCAL), "active": True},
                {"local_uuid": uuid.UUID(OTHER), "active": False}]
        self.assertEqual(prep.coverage(refs, []), (0, 1, 2))
        self.assertEqual(prep.coverage(refs, [{"local_uuid": LOCAL}]), (1, 0, 1))

    def test_v3_check_verifies_ids_and_usernames(self):
        saved = prep.public_identity(dict(USER, uuid=LOCAL), 2)
        remote = prep.public_identity(USER, 3)
        self.assertEqual(prep.verify_v3([saved], [remote]), [saved])
        self.assertEqual(prep.verify_v3([saved], []), [])
        with self.assertRaises(prep.PreparationError):
            prep.verify_v3([saved], [dict(remote, username="unexpected")])

    def test_backup_is_private_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.json"
            prep.save_export(path, "https://example.test/api", [])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                prep.save_export(path, "https://example.test/api", [])

    async def test_apply_changes_only_mapping_table_inside_transaction(self):
        conn = MagicMock()
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock()
        transaction.__aexit__ = AsyncMock(return_value=False)
        conn.transaction.return_value = transaction
        conn.execute = AsyncMock()
        candidate = prep.public_identity(dict(USER, uuid=LOCAL), 2)
        with patch.object(prep, "stored_identities", AsyncMock(return_value=[])):
            await prep.apply_mapping(conn, "https://example.test/api", [candidate])
        transaction.__aenter__.assert_awaited_once()
        transaction.__aexit__.assert_awaited_once_with(None, None, None)
        self.assertEqual(conn.execute.await_count, 3)
        for call in conn.execute.call_args_list:
            self.assertIn("remnawave_user_identities", call.args[0])
        self.assertEqual(conn.execute.call_args.args[1:4], ("https://example.test/api", uuid.UUID(LOCAL), 1234))

    async def test_conflict_aborts_apply_transaction(self):
        conn = MagicMock()
        transaction = MagicMock()
        transaction.__aenter__ = AsyncMock()
        transaction.__aexit__ = AsyncMock(return_value=False)
        conn.transaction.return_value = transaction
        conn.execute = AsyncMock()
        candidate = prep.public_identity(dict(USER, uuid=LOCAL), 2)
        with patch.object(prep, "stored_identities", AsyncMock(return_value=[dict(candidate, user_id=999)])):
            with self.assertRaises(prep.PreparationError):
                await prep.apply_mapping(conn, "https://example.test/api", [candidate])
        self.assertIs(transaction.__aexit__.call_args.args[0], prep.PreparationError)
        self.assertFalse(any("INSERT" in call.args[0] for call in conn.execute.call_args_list))


if __name__ == "__main__":
    unittest.main()
