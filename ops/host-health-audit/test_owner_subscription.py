"""Synthetic owner-only collection tests: no credentials or live requests."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("owner_subscription_under_test", Path(__file__).with_name("owner_subscription.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
UID = "83c2e902-ce09-443c-8659-b867a5311b7c"
OTHER = "6a2d3bbc-c8f1-46b0-a307-46fa9233736a"
SECRET = "DO-NOT-PRINT-OWNER-SUB-SECRET"


def schema(*, legacy=False, raw=True):
    paths = {}
    route = "/api/users/by-telegram-id/{telegramId}" if legacy else "/api/users/stream"
    params = [{"name": "telegramId", "in": "path" if legacy else "query"}]
    if not legacy:
        params += [{"name": name, "in": "query"} for name in ("size", "cursor")]
    account = {"type": "object", "properties": {"uuid": {"type": "string", "format": "uuid"},
                "telegramId": {"type": "integer"}}}
    users = {"type": "array", "items": account}
    def response(value):
        return {"200": {"content": {"application/json": {"schema": {"type": "object", "properties": {"response": value}}}}}}
    lookup = users if legacy else {"type": "object", "properties": {"users": users, "hasMore": {"type": "boolean"}}}
    paths[route] = {"get": {"parameters": params, "responses": response(lookup)}}
    if raw:
        paths["/api/subscriptions/by-short-uuid/{shortUuid}/raw"] = {"get": {"parameters": [
            {"name": "shortUuid", "in": "path"}, {"name": "withDisabledHosts", "in": "query"}],
            "responses": response({"type": "object", "properties": {
                "user": account, "resolvedProxyConfigs": {"type": "array", "items": {"type": "object"}}}})}}
    return {"openapi": "3.0.3", "info": {"version": "deployed-test-version"}, "paths": paths}


def user(uid=UID, owner=m.OWNER_ID, status="ACTIVE"):
    return {"uuid": uid, "telegramId": owner, "shortUuid": SECRET, "username": "Owner-test", "status": status,
            "expireAt": "2027-10-24T12:30:00Z"}


class OwnerSubscriptionTests(unittest.TestCase):
    def test_default_plan_has_no_files_credentials_or_network(self):
        output = io.StringIO()
        with patch.object(m, "collect") as collect, patch.object(m, "private_json") as read, \
             patch.object(m, "credentials") as credentials, patch.object(m, "OwnerAPI") as api, \
             contextlib.redirect_stdout(output):
            self.assertEqual(m.main([]), 0)
        for call in (collect, read, credentials, api):
            call.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])

    def test_routes_must_be_documented_before_adapter_construction(self):
        with patch.object(m, "private_json", return_value=schema(raw=False)), patch.object(m, "OwnerAPI") as api:
            with self.assertRaises(m.CollectionError):
                m.collect(Path("not-read"))
        api.assert_not_called()
        self.assertEqual(m.discover(schema())["lookup"], "stream")
        self.assertEqual(m.discover(schema(legacy=True))["lookup"], "legacy")
        unknown = schema(); del unknown["paths"]["/api/users/stream"]["get"]
        with self.assertRaises(m.CollectionError):
            m.discover(unknown)

    def test_numeric_identity_or_undocumented_response_stops_before_credentials(self):
        for change in ("numeric", "no_response", "old_raw_hosts"):
            document = schema()
            lookup = document["paths"]["/api/users/stream"]["get"]
            raw = document["paths"]["/api/subscriptions/by-short-uuid/{shortUuid}/raw"]["get"]
            if change == "numeric":
                record = lookup["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["response"]["properties"]["users"]["items"]
                record["properties"].pop("uuid")
                record["properties"]["id"] = {"type": "integer"}
            elif change == "no_response":
                raw.pop("responses")
            else:
                fields = raw["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["response"]["properties"]
                fields["rawHosts"] = fields.pop("resolvedProxyConfigs")
            with self.subTest(change=change), patch.object(m, "private_json", return_value=document), \
                 patch.object(m, "OwnerAPI") as api:
                with self.assertRaises(m.CollectionError):
                    m.collect(Path("not-read"))
                api.assert_not_called()

    def test_lookup_is_owner_filtered_bounded_and_never_accepts_foreign_rows(self):
        api = Mock()
        api.get.side_effect = [{"users": [user()], "hasMore": True, "nextCursor": "private-cursor"},
                               {"users": [user(OTHER)], "hasMore": False, "nextCursor": None}]
        rows = m.lookup(api, m.discover(schema()))
        self.assertEqual(len(rows), 2)
        self.assertEqual(api.get.call_args_list[0].args, ("/users/stream?telegramId=6910097562&size=25",))
        self.assertIn("telegramId=6910097562", api.get.call_args_list[1].args[0])
        api.get.side_effect = None
        api.get.return_value = {"users": [user(owner=42)], "hasMore": False}
        with self.assertRaises(m.CollectionError):
            m.lookup(api, m.discover(schema()))

    def test_legacy_is_only_used_when_documented_and_requires_exact_owner(self):
        api = Mock(); api.get.return_value = [user()]
        self.assertEqual(m.lookup(api, m.discover(schema(legacy=True)))[0]["uuid"], UID)
        api.get.assert_called_once_with("/users/by-telegram-id/6910097562")
        api.get.return_value = {"users": [user()]}
        with self.assertRaises(m.CollectionError):
            m.lookup(api, m.discover(schema(legacy=True)))

    def test_zero_or_multiple_active_accounts_only_emit_safe_selection_preview(self):
        for rows in ([], [user(), user(OTHER)]):
            with self.subTest(count=len(rows)):
                api = Mock(); api.get.return_value = {"users": rows, "hasMore": False}
                with patch.object(m, "private_json", return_value=schema()), patch.object(m, "OwnerAPI", return_value=api), \
                     patch.object(m, "save_private") as save:
                    result = m.collect(Path("not-read"))
                self.assertTrue(result["selection_required"])
                self.assertFalse(result["collected"])
                self.assertNotIn(SECRET, json.dumps(result))
                self.assertNotIn("shortUuid", json.dumps(result))
                save.assert_not_called()
                self.assertEqual(api.get.call_count, 1)

    def test_raw_collection_is_selected_owner_only_and_never_prints_payload(self):
        api = Mock()
        payload = {"user": user(), "headers": {"authorization": SECRET},
                   "resolvedProxyConfigs": [{"password": SECRET, "metadata": {"uuid": OTHER}}]}
        api.get.side_effect = [{"users": [user(), user(OTHER)], "hasMore": False}, payload]
        with patch.object(m, "private_json", return_value=schema()), patch.object(m, "OwnerAPI", return_value=api), \
             patch.object(m, "save_private", return_value=(Path("/private/tmp/private/normalized.private.json"), "hash")) as save:
            result = m.collect(Path("not-read"), UID)
        self.assertEqual(api.selected_short, SECRET)
        self.assertEqual(api.get.call_args_list[1].args, ("/subscriptions/by-short-uuid/" + SECRET + "/raw?withDisabledHosts=false",))
        self.assertEqual(save.call_args.args[0]["response"], payload)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertFalse(result["executable_xray_json"])
        self.assertTrue(result["request_device_metadata_may_change"])

    def test_foreign_selected_user_or_raw_identity_never_saves(self):
        for selected, payload in ((OTHER, {"user": user(), "resolvedProxyConfigs": []}),
                                  (UID, {"user": user(OTHER), "resolvedProxyConfigs": []}),
                                  (UID, {"user": user(owner=42), "resolvedProxyConfigs": []}),
                                  (UID, {"user": user(), "rawHosts": []})):
            with self.subTest(selected=selected, fields=list(payload)):
                api = Mock(); api.get.side_effect = [{"users": [user()], "hasMore": False}, payload]
                with patch.object(m, "private_json", return_value=schema()), patch.object(m, "OwnerAPI", return_value=api), \
                     patch.object(m, "save_private") as save:
                    with self.assertRaises(m.CollectionError):
                        m.collect(Path("not-read"), selected)
                save.assert_not_called()

    def test_get_adapter_rejects_other_users_mutations_and_other_subscription(self):
        api = object.__new__(m.OwnerAPI)
        api.contract = m.discover(schema()); api.selected_short = "only-this-owner"
        api.base = "https://panel.wayspn.online/api"; api.token = SECRET
        api.opener = Mock()
        for path in ("/users", "/users/stream?telegramId=42&size=25", "/users/stream?telegramId=6910097562&size=25&extra=yes",
                     "/users/by-telegram-id/42", "/subscriptions/by-short-uuid/foreign/raw?withDisabledHosts=false",
                     "/subscriptions/by-short-uuid/only-this-owner/raw?withDisabledHosts=true", "https://evil.invalid/users/stream"):
            with self.subTest(path=path), self.assertRaises(m.CollectionError):
                api.get(path)
        api.opener.open.assert_not_called()

    def test_private_storage_and_openapi_reject_unsafe_metadata(self):
        path, digest = m.save_private({"private": SECRET})
        self.addCleanup(lambda: shutil.rmtree(path.parent))
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(m.private_json(path), {"private": SECRET})
        self.assertEqual(len(digest), 64)
        path.chmod(0o644)
        with self.assertRaises(m.CollectionError):
            m.private_json(path)
        path.chmod(0o600)
        alias = path.parent / "link"
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            m.private_json(alias)
        alias.unlink(); os.link(path, alias)
        with self.assertRaises(m.CollectionError):
            m.private_json(path)

    def test_errors_never_emit_secret_exception_text(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(m, "collect", side_effect=RuntimeError(SECRET)), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(m.main(["--collect", "--openapi", "/private/tmp/not-read"]), 2)
        self.assertNotIn(SECRET, output.getvalue() + error.getvalue())
        self.assertEqual(output.getvalue(), "")

    def test_owner_diagnostic_legacy_only_saves_private_rows_and_prints_projection(self):
        api = Mock(); api.get.return_value = [user()]
        with patch.object(m, "OwnerAPI", return_value=api), patch.object(m, "private_json") as read, \
             patch.object(m, "save_private", return_value=(Path("/private/tmp/private/owner-lookup.private.json"), "hash")) as save:
            result = m.discover_owner()
        api.get.assert_called_once_with("/users/by-telegram-id/6910097562")
        read.assert_not_called()
        self.assertEqual(result["count"], 1)
        self.assertEqual(set(result["accounts"][0]), {"accountID", "username", "status", "expiry"})
        self.assertEqual(result["accounts"][0]["accountID"], UID)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(save.call_args.args[0]["users"], [user()])
        self.assertEqual(save.call_args.args[1], "owner-lookup.private.json")

    def test_owner_diagnostic_only_404_falls_back_to_filtered_numeric_stream(self):
        numeric = user(); numeric.pop("uuid"); numeric["id"] = 17
        api = Mock(); api.get.side_effect = [m.PanelHTTPError(404), {
            "users": [numeric], "hasMore": False, "nextCursor": None}]
        with patch.object(m, "OwnerAPI", return_value=api), \
             patch.object(m, "save_private", return_value=(Path("/private/tmp/private/owner-lookup.private.json"), "hash")):
            result = m.discover_owner()
        self.assertEqual(result["accounts"][0]["accountID"], 17)
        self.assertEqual([call.args[0] for call in api.get.call_args_list], [
            "/users/by-telegram-id/6910097562", "/users/stream?telegramId=6910097562&size=25"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_owner_diagnostic_never_falls_back_after_other_errors_or_200_unknown_wrapper(self):
        for error in (m.PanelHTTPError(401), m.PanelHTTPError(403), m.PanelHTTPError(429),
                      m.PanelHTTPError(500), RuntimeError(SECRET), {"users": [user()]}):
            api = Mock()
            if isinstance(error, Exception):
                api.get.side_effect = error
            else:
                api.get.return_value = error
            with self.subTest(error=type(error).__name__), patch.object(m, "OwnerAPI", return_value=api), \
                 patch.object(m, "save_private") as save:
                with self.assertRaises(Exception):
                    m.discover_owner()
                save.assert_not_called()
                self.assertEqual(api.get.call_count, 1)

    def test_owner_diagnostic_foreign_duplicate_unknown_or_paginated_rows_never_save(self):
        for page in ({"users": [user(owner=42)], "hasMore": False, "nextCursor": None},
                     {"users": [user(), user()], "hasMore": False, "nextCursor": None},
                     {"users": [user()], "hasMore": True, "nextCursor": "opaque-cursor"},
                     {"users": [user()], "hasMore": False, "nextCursor": "unexpected-cursor"},
                     {"users": [user()], "hasMore": False},
                     {"users": [user()], "hasMore": False, "nextCursor": None, "unknown": SECRET},
                     {"users": [{**user(), "status": "UNKNOWN"}], "hasMore": False, "nextCursor": None}):
            api = Mock(); api.get.side_effect = [m.PanelHTTPError(404), page]
            with self.subTest(keys=list(page)), patch.object(m, "OwnerAPI", return_value=api), \
                 patch.object(m, "save_private") as save:
                with self.assertRaises(Exception):
                    m.discover_owner()
                save.assert_not_called()
                self.assertEqual(api.get.call_count, 2)

    def test_owner_diagnostic_rejects_nonpositive_numeric_identity_and_foreign_legacy_rows(self):
        for identity in (0, -1, True, 1.0, "17"):
            numeric = user(); numeric.pop("uuid"); numeric["id"] = identity
            with self.subTest(identity=identity), self.assertRaises(m.CollectionError):
                m.diagnostic_account(numeric)
        api = Mock(); api.get.return_value = [user(), user(OTHER, owner=42)]
        with patch.object(m, "OwnerAPI", return_value=api), patch.object(m, "save_private") as save:
            with self.assertRaises(m.CollectionError):
                m.discover_owner()
        save.assert_not_called()
        self.assertEqual(api.get.call_count, 1)

    def test_owner_diagnostic_mode_rejects_raw_cursor_broad_and_foreign_routes(self):
        api = object.__new__(m.OwnerAPI)
        api.contract = {"lookup": "owner-diagnostic", "cursor": False}
        api.selected_short = "even-selected-cannot-fetch"
        api.base = m.PANEL_ORIGIN; api.token = SECRET; api.opener = Mock()
        for path in ("/users", "/users/stream?telegramId=6910097562&size=25&cursor=x",
                     "/users/stream?telegramId=6910097562&size=25&cursor=",
                     "/users/stream?telegramId=6910097562&size=25&extra=", "/users/by-telegram-id/42",
                     "/subscriptions/by-short-uuid/even-selected-cannot-fetch/raw?withDisabledHosts=false"):
            with self.subTest(path=path), self.assertRaises(m.CollectionError):
                api.get(path)
        api.opener.open.assert_not_called()

    def test_owner_diagnostic_main_needs_no_openapi_and_suppresses_failure(self):
        output = io.StringIO()
        with patch.object(m, "discover_owner", return_value={"count": 0, "accounts": []}) as discover, \
             patch.object(m, "collect") as collect, patch.object(m, "private_json") as read, contextlib.redirect_stdout(output):
            self.assertEqual(m.main(["--discover-owner"]), 0)
        discover.assert_called_once_with(); collect.assert_not_called(); read.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"count": 0, "accounts": []})
        output, error = io.StringIO(), io.StringIO()
        with patch.object(m, "discover_owner", side_effect=RuntimeError(SECRET)), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(m.main(["--discover-owner"]), 2)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(SECRET, error.getvalue())


if __name__ == "__main__":
    unittest.main()
