"""Local pending-config preparation; artificial inputs and isolated files only."""
import base64
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings

from scripts import partner_enable_owner_pilot as prepare
from services.partner_runtime import PartnerRuntimeSettings


SHOP = "123456"
SECRET = "live_artificial-only-merchant-key"
TOKEN = "123456789:" + "T" * 35
DB = ("postgresql://spn_partner_runtime.crrmaqiakrqpfvhiwtkq:artificial-db-password-123456"
      "@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres")


def source_config():
    return {"runtime": {
        "mode": "staging", "staging_isolation_confirmed": True,
        "database_isolation_confirmed": True, "database_api_disabled_confirmed": True,
        "allow_staging_network": True, "allow_staging_writes": True,
        "enable_http": True, "enable_manager_webhook": True,
        "manager_allowed_user_ids": [prepare.OWNER_ID],
        "database_dsn": DB, "database_tls": True,
        "database_ca_file": "/etc/spn-partner-ca/supabase.crt",
        "database_connection_mode": "session", "database_project_ref": prepare.PROJECT_REF,
        "base_domain": "sub.wayspn.online", "manager_origin": prepare.ORIGIN,
        "webhook_origin": prepare.ORIGIN, "manager_bot_token": TOKEN,
        "manager_bot_username": "spn_franchise_bot",
        "encryption_keys": [base64.urlsafe_b64encode(b"p" * 32).decode()],
        "webhook_key_hex": "ab" * 32, "subscription_key_hex": "cd" * 32,
        "interval_seconds": 45, "pass_limit": 10,
    }, "staging_addon_catalog": None}


class OwnerPilotPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.private = Path(temporary.name) / "private"
        self.private.mkdir(mode=0o700)
        self.source = self.private / "staging.json"
        self.output = self.private / "staging.owner-entry-pilot.pending.json"
        self.merchant = self.private / "yookassa.owner-entry-pilot.json"
        for name, value in (("ROOT_UID", os.geteuid()), ("CONFIG_PATH", self.source),
                            ("OUTPUT_PATH", self.output), ("MERCHANT_PATH", self.merchant)):
            patcher = patch.object(prepare, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(prepare.sys, "platform", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_source(self, value=None):
        raw = json.dumps(source_config() if value is None else value, ensure_ascii=False, indent=2).encode() + b"\n"
        self.source.write_bytes(raw)
        self.source.chmod(0o600)
        return raw

    def args(self, *extras):
        return ["--prepare", "--config", str(self.source), "--output", str(self.output), *extras]

    def candidate(self, config=None):
        return prepare.make_candidate(source_config() if config is None else config,
                                      shop_id=SHOP, secret_key=SECRET)

    def test_pure_builder_preserves_input_secrets_and_existing_noncapability_values(self):
        source = source_config()
        before = deepcopy(source)
        candidate = self.candidate(source)
        self.assertEqual(source, before)
        self.assertIsNot(candidate, source)
        for key in prepare.PRESERVED:
            self.assertEqual(candidate["runtime"][key], before["runtime"][key])
        settings = PartnerRuntimeSettings.from_mapping(candidate["runtime"])
        self.assertTrue(settings.preflight()["configured"])
        self.assertFalse(settings.preflight()["production_ready"])
        for key in prepare.OFF_FLAGS:
            self.assertIs(getattr(settings, key), False)
        for key in ("enable_owner_entry_pilot", "enable_billing", "enable_launches",
                    "enable_customer_webhooks", "enable_worker", "management_enabled",
                    "finance_enabled", "branding_enabled"):
            self.assertIs(getattr(settings, key), True)
        self.assertFalse(settings.yookassa_test_mode)
        self.assertEqual(settings.operator_ids, (prepare.OWNER_ID,))
        self.assertEqual(settings.admin_recipient_id, prepare.OWNER_ID)
        self.assertEqual(settings.interval_seconds, 45)
        self.assertEqual(settings.pass_limit, 10)
        for secret in (TOKEN, DB, SECRET, source["runtime"]["encryption_keys"][0]):
            self.assertNotIn(secret, repr(candidate))
            self.assertNotIn(secret, repr(settings))

    def test_unknown_fields_wrong_owner_database_and_prior_commerce_refused(self):
        changes = (
            {"unknown_secret_setting": SECRET}, {"manager_allowed_user_ids": None},
            {"manager_allowed_user_ids": [prepare.OWNER_ID, 42]},
            {"operator_ids": [42]}, {"admin_recipient_id": 42},
            {"mode": "disabled"}, {"enable_billing": True}, {"enable_launches": True},
            {"enable_worker": True}, {"enable_customer_webhooks": True},
            {"enable_provisioning": True}, {"enable_public_gateway": True},
            {"yookassa_secret_key": SECRET}, {"remnawave_api_token": SECRET},
            {"database_project_ref": "anotherprojectref"},
            {"database_dsn": DB.replace("spn_partner_runtime.", "postgres.")},
            {"manager_origin": "https://other.example.com"},
            {"manager_bot_username": "OtherManagerBot"},
        )
        for change in changes:
            source = source_config()
            source["runtime"].update(change)
            with self.subTest(fields=list(change)), self.assertRaises(prepare.PilotPreparationError) as raised:
                self.candidate(source)
            for secret in (TOKEN, DB, SECRET):
                self.assertNotIn(secret, str(raised.exception))
        source = source_config()
        source["extra"] = SECRET
        with self.assertRaises(prepare.PilotPreparationError):
            self.candidate(source)

    def test_merchant_inputs_strict_and_no_secret_in_failure(self):
        for shop, key in ((123, SECRET), ("not-id", SECRET), (SHOP, "short"),
                          (SHOP, "key contains whitespace"), (True, SECRET)):
            with self.subTest(shop=shop), self.assertRaises(prepare.PilotPreparationError) as raised:
                prepare.make_candidate(source_config(), shop_id=shop, secret_key=key)
            self.assertNotIn(str(key), str(raised.exception))

    def test_exclusive_pending_write_leaves_master_bytes_untouched(self):
        original = self.write_source()
        result = prepare.prepare_pending(merchant_reader=lambda: (SHOP, SECRET))
        self.assertEqual(result, self.output)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(self.output.stat().st_uid, os.geteuid())
        self.assertEqual(self.output.stat().st_nlink, 1)
        pending = self.output.read_bytes()
        self.assertEqual(json.loads(pending)["runtime"]["yookassa_secret_key"], SECRET)
        with self.assertRaises(prepare.PilotPreparationError):
            prepare.prepare_pending(merchant_reader=lambda: self.fail("Existing pending must stop before input"))
        self.assertEqual(self.output.read_bytes(), pending)
        self.assertEqual(self.source.read_bytes(), original)

    def test_partial_write_removes_only_new_pending_and_keeps_master(self):
        original = self.write_source()
        with patch.object(prepare.os, "write", side_effect=OSError(SECRET)):
            with self.assertRaises(OSError):
                prepare.prepare_pending(merchant_reader=lambda: (SHOP, SECRET))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), original)

    def test_unsafe_source_parent_hardlink_and_symlink_stop_without_pending(self):
        original = self.write_source()
        for path, mode in ((self.source, 0o640), (self.private, 0o750)):
            old = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode)
            with self.subTest(path=path.name), self.assertRaises(prepare.PilotPreparationError):
                prepare.prepare_pending(merchant_reader=lambda: self.fail("Unsafe source must stop before input"))
            path.chmod(old)
        link = self.private / "source-link.json"
        os.link(self.source, link)
        with self.assertRaises(prepare.PilotPreparationError):
            prepare.prepare_pending(merchant_reader=lambda: (SHOP, SECRET))
        link.unlink()
        self.source.rename(link)
        self.source.symlink_to(link)
        with self.assertRaises(prepare.PilotPreparationError):
            prepare.prepare_pending(merchant_reader=lambda: (SHOP, SECRET))
        self.assertEqual(link.read_bytes(), original)
        self.assertFalse(self.output.exists())

    def test_duplicate_json_keys_rejected_and_master_change_during_prompt_aborts(self):
        self.source.write_text('{"runtime":{},"runtime":{},"staging_addon_catalog":null}')
        self.source.chmod(0o600)
        with self.assertRaises(ValueError):
            prepare.prepare_pending(merchant_reader=lambda: self.fail("Duplicate source must stop before input"))
        original = self.write_source()

        def change_master():
            self.source.write_bytes(original + b"\n")
            return SHOP, SECRET

        with self.assertRaises(prepare.PilotPreparationError):
            prepare.prepare_pending(merchant_reader=change_master)
        self.assertEqual(self.source.read_bytes(), original + b"\n")
        self.assertFalse(self.output.exists())

    def test_explicit_private_merchant_file_has_only_two_keys(self):
        original = self.write_source()
        self.merchant.write_text(json.dumps({"shop_id": SHOP, "secret_key": SECRET}))
        self.merchant.chmod(0o600)
        with patch.object(prepare.getpass, "getpass") as prompt:
            prepare.prepare_pending(merchant_file=True)
        prompt.assert_not_called()
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(json.loads(self.output.read_bytes())["runtime"]["yookassa_shop_id"], SHOP)

    def test_bad_merchant_file_extras_duplicates_and_permissions_rejected(self):
        self.write_source()
        for raw in (json.dumps({"shop_id": SHOP, "secret_key": SECRET, "extra": 1}),
                    '{"shop_id":"123","shop_id":"456","secret_key":"artificial-key"}'):
            self.merchant.write_text(raw)
            self.merchant.chmod(0o600)
            with self.assertRaises(ValueError):
                prepare.prepare_pending(merchant_file=True)
            self.assertFalse(self.output.exists())
        self.merchant.write_text(json.dumps({"shop_id": SHOP, "secret_key": SECRET}))
        self.merchant.chmod(0o640)
        with self.assertRaises(prepare.PilotPreparationError):
            prepare.prepare_pending(merchant_file=True)

    def test_default_plan_no_secret_reads_inputs_or_mutations(self):
        output = io.StringIO()
        with patch.object(prepare.os, "open", side_effect=AssertionError("No file I/O in plan")), \
                patch.object(prepare.getpass, "getpass") as prompt, contextlib.redirect_stdout(output):
            self.assertEqual(prepare.main([]), 0)
        prompt.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        self.assertFalse(self.output.exists())

    def test_cli_requires_exact_explicit_paths_and_root_before_reading(self):
        with patch.object(prepare.os, "open", side_effect=AssertionError("Must not read")), \
                contextlib.redirect_stderr(io.StringIO()):
            for args in (["--prepare"], self.args("--config", "/tmp/elsewhere.json"),
                         self.args("--output", str(self.source)),
                         self.args("--merchant-file", "/tmp/arbitrary.json")):
                self.assertEqual(prepare.main(args), 2)
            with patch.object(prepare.os, "geteuid", return_value=999999):
                self.assertEqual(prepare.main(self.args()), 2)

    def test_cli_hidden_input_success_no_values_printed_no_master_activation(self):
        original = self.write_source()
        output = io.StringIO()
        with patch.object(prepare.sys.stdin, "isatty", return_value=True), \
                patch.object(prepare.getpass, "getpass", side_effect=[SHOP, SECRET]) as prompt, \
                contextlib.redirect_stdout(output):
            self.assertEqual(prepare.main(self.args()), 0)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(json.loads(output.getvalue())["payments_enabled"])
        for secret in (SHOP, SECRET, TOKEN, DB):
            self.assertNotIn(secret, output.getvalue())

    def test_unknown_cli_credential_argument_is_never_echoed(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
            self.assertEqual(prepare.main(["--secret-key", SECRET]), 2)
        self.assertNotIn(SECRET, output.getvalue())
        self.assertFalse(self.output.exists())

    def test_pipe_and_getpass_warning_never_fall_back_to_echoed_input(self):
        original = self.write_source()
        with patch.object(prepare.sys.stdin, "isatty", return_value=False), \
                patch.object(prepare.getpass, "getpass") as prompt, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(prepare.main(self.args()), 2)
        prompt.assert_not_called()

        def cannot_hide(*args):
            warnings.warn("cannot hide", prepare.getpass.GetPassWarning)
            self.fail("Warning must abort before visible input")

        with patch.object(prepare.sys.stdin, "isatty", return_value=True), \
                patch.object(prepare.getpass, "getpass", side_effect=cannot_hide), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(prepare.main(self.args()), 2)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
