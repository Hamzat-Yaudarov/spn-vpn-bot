"""Synthetic Remnawave responses only; no network calls or engine execution."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from spn_ip_failover import refresh


ADMIN = "01234567-89ab-4cde-8123-456789abcdef"
FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"
THIRD = "33333333-3333-4333-8333-333333333333"
CLIENT_SECRET = "synthetic-client-id-never-in-coverage"
SHORT_SECRET = "synthetic-short-subscription-secret"
TOKEN_SECRET = "synthetic-api-token"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def user():
    return {"id": 2, "username": "Admin", "status": "ACTIVE", "shortUuid": SHORT_SECRET,
            "expireAt": "2099-01-01T00:00:00Z", "password": "unused-account-secret"}


def source(uuid=FIRST, address="8.8.8.8", disabled=False):
    return {"protocol": "vless", "protocolOptions": {"id": CLIENT_SECRET, "encryption": "none"},
            "address": address, "port": 443, "transport": "ws", "transportOptions": {"path": "/probe"},
            "security": "tls", "securityOptions": {"serverName": "vpn.example.com"},
            "metadata": {"uuid": uuid, "remark": "Public VPN name", "isDisabled": disabled,
                         "isHidden": False, "rawInbound": {"privateKey": "unused-server-secret"}}}


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.audit = refresh.load_audit_helper()
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.directory.chmod(0o700)
        self.output = self.directory / "routes.json"
        self.coverage = self.directory / "coverage.json"
        self.config_path = self.directory / "refresh.json"
        self.config = {"panel_url": "https://panel.example.com/api", "token_env": "REMNAWAVE_API_TOKEN",
                       "username": "Admin", "xray": "/linux/opt/not-installed-locally/xray",
                       "sha256": "a" * 64, "output": str(self.output), "coverage_output": str(self.coverage)}
        self.api = Mock()
        self.raw = {"user": user(), "resolvedProxyConfigs": [source()], "headers": {"unused": SHORT_SECRET},
                    "convertedUserInfo": {"clientKey": CLIENT_SECRET}}

    def tearDown(self):
        self.temporary.cleanup()

    def run_refresh(self, raw=None, account=None):
        self.api._request.side_effect = [user() if account is None else account, self.raw if raw is None else raw]
        with patch.object(refresh, "Remnawave", return_value=self.api) as client, \
             patch.object(self.audit, "pinned_engine", side_effect=AssertionError("Converter must not stat the engine")):
            result = refresh.refresh(self.audit, self.config, config_path=self.config_path,
                                     environ={"REMNAWAVE_API_TOKEN": TOKEN_SECRET}, now=NOW)
        client.assert_called_once_with("https://panel.example.com/api", TOKEN_SECRET)
        return result

    def preserve_old(self):
        self.audit.write_private(self.output, {"old": "routes"})
        self.audit.write_private(self.coverage, {"old": "coverage"})

    def assert_old(self):
        self.assertEqual(json.loads(self.output.read_text()), {"old": "routes"})
        self.assertEqual(json.loads(self.coverage.read_text()), {"old": "coverage"})

    def test_refresh_uses_two_gets_and_retains_only_current_supported_routes(self):
        raw = deepcopy(self.raw)
        disabled = source(SECOND, "1.1.1.1", True)
        unsupported = source(THIRD, "9.9.9.9")
        unsupported["protocol"] = "wireguard"
        raw["resolvedProxyConfigs"] += [disabled, unsupported]
        self.preserve_old()
        result = self.run_refresh(raw)
        self.assertEqual(self.api._request.call_args_list, [call("GET", "/users/by-username/Admin"),
            call("GET", "/subscriptions/by-short-uuid/" + SHORT_SECRET + "/raw?withDisabledHosts=true")])
        routes = self.audit.protected_json(self.output)
        coverage = self.audit.protected_json(self.coverage)
        self.assertEqual(routes["control_mode"], "direct_https")
        self.assertEqual(set(routes["routes"]), {FIRST, SECOND})
        self.assertEqual(routes["routes"][FIRST]["probe"]["settings"]["vnext"][0]["address"], "8.8.8.8")
        self.assertEqual(routes["routes"][SECOND]["probe"]["settings"]["vnext"][0]["address"], "1.1.1.1")
        self.assertEqual(routes["xray"], self.config["xray"])
        self.assertEqual(result["counts"], {"total": 3, "supported": 2, "unsupported": 1, "disabled": 1})
        self.assertTrue(coverage["hosts"][1]["disabled"])
        self.assertEqual(coverage["hosts"][2]["unsupportedReason"], "unsupported_resolved_outbound")
        self.assertEqual(routes["refreshedAt"], coverage["refreshedAt"])
        self.assertEqual(set(result), {"refreshedAt", "counts"})
        public = json.dumps(coverage) + json.dumps(result)
        for secret in (CLIENT_SECRET, SHORT_SECRET, TOKEN_SECRET, "unused-server-secret", "unused-account-secret"):
            self.assertNotIn(secret, public)
        route_text = self.output.read_text()
        self.assertIn(CLIENT_SECRET, route_text)
        for excluded in (SHORT_SECRET, TOKEN_SECRET, "unused-server-secret", "unused-account-secret", "convertedUserInfo", "rawInbound"):
            self.assertNotIn(excluded, route_text)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.coverage.stat().st_mode & 0o777, 0o600)

    def test_refreshed_address_replaces_old_snapshot(self):
        self.run_refresh()
        raw = deepcopy(self.raw)
        raw["resolvedProxyConfigs"][0]["address"] = "1.1.1.1"
        self.api.reset_mock()
        self.run_refresh(raw)
        saved = self.audit.protected_json(self.output)
        self.assertEqual(saved["routes"][FIRST]["probe"]["settings"]["vnext"][0]["address"], "1.1.1.1")

    def test_selected_hosts_exclude_bypass_from_conversion_and_coverage(self):
        self.config['selected_host_uuids'] = [FIRST]
        raw = deepcopy(self.raw)
        bypass = source(SECOND, '1.1.1.1')
        bypass['metadata']['remark'] = 'Excluded VK bypass'
        bypass['protocol'] = 'unsupported-bypass'
        raw['resolvedProxyConfigs'].append(bypass)
        result = self.run_refresh(raw)
        self.assertEqual(result['counts'], {'total': 1, 'supported': 1, 'unsupported': 0, 'disabled': 0})
        self.assertEqual(set(json.loads(self.output.read_text())['routes']), {FIRST})
        self.assertNotIn('Excluded VK bypass', self.coverage.read_text())
        self.assertNotIn(SECOND, self.coverage.read_text())

    def test_missing_or_unsupported_selected_host_preserves_snapshot(self):
        self.preserve_old()
        self.config['selected_host_uuids'] = [FIRST, SECOND]
        with self.assertRaises(ValueError):
            self.run_refresh()
        self.assert_old()
        raw = deepcopy(self.raw)
        bad = source(SECOND); bad['protocol'] = 'wireguard'
        raw['resolvedProxyConfigs'].append(bad)
        with self.assertRaises(ValueError):
            self.run_refresh(raw)
        self.assert_old()

    def test_invalid_selected_list_fails_before_api(self):
        with patch.object(refresh, 'Remnawave') as api:
            for selected in ([], [FIRST, FIRST], ['bad'], FIRST, [True]):
                with self.subTest(selected=selected), self.assertRaises(ValueError):
                    refresh.refresh(self.audit, {**self.config, 'selected_host_uuids': selected},
                                    environ={'REMNAWAVE_API_TOKEN': TOKEN_SECRET}, now=NOW)
        api.assert_not_called()

    def test_duplicate_and_empty_supported_inventory_preserve_previous_files(self):
        self.preserve_old()
        duplicate = deepcopy(self.raw)
        duplicate["resolvedProxyConfigs"] += [source(FIRST)]
        unsupported = deepcopy(self.raw)
        unsupported["resolvedProxyConfigs"][0]["protocol"] = "wireguard"
        for raw in (duplicate, unsupported, {"user": user(), "resolvedProxyConfigs": []}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.run_refresh(raw)
            self.assert_old()
            self.api.reset_mock()

    def test_unknown_schema_or_foreign_user_never_overwrites_inventory(self):
        self.preserve_old()
        cases = []
        wrong = deepcopy(self.raw)
        wrong["user"]["id"] = 3
        cases.append(wrong)
        wrong = deepcopy(self.raw)
        wrong["resolvedProxyConfigs"][0]["metadata"]["uuid"] = "not-a-uuid"
        cases.append(wrong)
        wrong = deepcopy(self.raw)
        wrong["resolvedProxyConfigs"][0]["metadata"].pop("remark")
        cases.append(wrong)
        wrong = deepcopy(self.raw)
        wrong["resolvedProxyConfigs"][0]["metadata"]["isDisabled"] = "false"
        cases.append(wrong)
        wrong = deepcopy(self.raw)
        wrong["resolvedProxyConfigs"][0]["isDisabled"] = True
        cases.append(wrong)
        cases.extend(({"user": user(), "unknownProxyConfigs": []}, {"resolvedProxyConfigs": [source()]}))
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.run_refresh(raw)
            self.assert_old()
            self.api.reset_mock()

    def test_inactive_expired_or_ambiguous_admin_stops_before_raw_request(self):
        self.preserve_old()
        for changes in ({"status": "DISABLED"}, {"username": "Other"}, {"expireAt": "2026-11-14T11:59:59Z"},
                        {"expireAt": "2027-01-01T00:00:00"}, {"shortUuid": "unsafe/private"},
                        {"id": True}, {"id": 0}, {"uuid": "not-a-uuid"}):
            account = {**user(), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_refresh(account=account)
            self.assertEqual(self.api._request.call_count, 1)
            self.assert_old()
            self.api.reset_mock()

    def test_bound_inventory_and_name_fallback(self):
        raw = deepcopy(self.raw)
        metadata = raw["resolvedProxyConfigs"][0]["metadata"]
        metadata.pop("remark")
        raw["resolvedProxyConfigs"][0]["finalRemark"] = "Fallback public name"
        metadata.pop("isDisabled")
        self.run_refresh(raw)
        coverage = self.audit.protected_json(self.coverage)
        self.assertEqual(coverage["hosts"][0]["name"], "Fallback public name")
        self.assertIsNone(coverage["hosts"][0]["disabled"])
        previous = self.output.read_bytes()
        raw["resolvedProxyConfigs"] *= 513
        with self.assertRaises(ValueError):
            self.run_refresh(raw)
        self.assertEqual(self.output.read_bytes(), previous)

    def test_raw_identity_can_use_matching_short_uuid_when_id_is_absent(self):
        raw = deepcopy(self.raw)
        raw["user"].pop("id")
        self.run_refresh(raw)
        previous = self.output.read_bytes()
        raw["user"]["shortUuid"] = "another-subscription"
        with self.assertRaises(ValueError):
            self.run_refresh(raw)
        self.assertEqual(self.output.read_bytes(), previous)

    def test_invalid_config_and_unsafe_targets_fail_before_api_construction(self):
        changes = ({"username": "Other"}, {"sha256": "not-a-pin"}, {"extra": True},
                   {"panel_url": "https://user:password@panel.example.com"}, {"panel_url": "http://panel.example.com"},
                   {"xray": "relative-xray"}, {"output": str(self.coverage)}, {"output": str(self.config_path)},
                   {"output": self.config["xray"]}, {"token_env": "INVALID-NAME"})
        with patch.object(refresh, "Remnawave") as api:
            for change in changes:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    refresh.refresh(self.audit, {**self.config, **change}, config_path=self.config_path,
                                    environ={"REMNAWAVE_API_TOKEN": TOKEN_SECRET}, now=NOW)
        api.assert_not_called()
        self.coverage.symlink_to(self.output)
        with patch.object(refresh, "Remnawave") as api, self.assertRaises(ValueError):
            refresh.refresh(self.audit, self.config, environ={"REMNAWAVE_API_TOKEN": TOKEN_SECRET}, now=NOW)
        api.assert_not_called()

    def test_api_failure_preserves_both_files(self):
        self.preserve_old()
        self.api._request.side_effect = [user(), RuntimeError(SHORT_SECRET)]
        with patch.object(refresh, "Remnawave", return_value=self.api), self.assertRaises(RuntimeError):
            refresh.refresh(self.audit, self.config, environ={"REMNAWAVE_API_TOKEN": TOKEN_SECRET}, now=NOW)
        self.assert_old()

    def test_cli_reads_actual_protected_config_and_redacts_errors(self):
        self.audit.write_private(self.config_path, self.config)
        self.api._request.side_effect = [user(), self.raw]
        with patch.dict(refresh.os.environ, {"REMNAWAVE_API_TOKEN": TOKEN_SECRET}), \
             patch.object(refresh, "Remnawave", return_value=self.api), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(refresh.main(["--config", str(self.config_path)]), 0)
        self.assertEqual(set(json.loads(output.getvalue())), {"refreshedAt", "counts"})
        self.assertEqual(errors.getvalue(), "")
        previous = self.output.read_bytes()
        self.config_path.chmod(0o644)
        with patch.object(refresh, "Remnawave") as api, redirect_stdout(io.StringIO()) as output, \
             redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(refresh.main(["--config", str(self.config_path)]), 1)
        api.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(SHORT_SECRET, errors.getvalue())
        self.assertEqual(self.output.read_bytes(), previous)

    def test_cli_api_and_argument_errors_never_print_short_uuid_or_token(self):
        self.audit.write_private(self.config_path, self.config)
        self.api._request.side_effect = RuntimeError("https://panel.example.com/" + SHORT_SECRET + TOKEN_SECRET)
        with patch.dict(refresh.os.environ, {"REMNAWAVE_API_TOKEN": TOKEN_SECRET}), \
             patch.object(refresh, "Remnawave", return_value=self.api), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(refresh.main(["--config", str(self.config_path)]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(SHORT_SECRET, errors.getvalue())
        self.assertNotIn(TOKEN_SECRET, errors.getvalue())
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(refresh.main(["--unknown", SHORT_SECRET]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(SHORT_SECRET, errors.getvalue())


if __name__ == "__main__":
    unittest.main()
