"""Private config transition with artificial secrets and temporary files only."""

import contextlib
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.partner_setup import build_config
from services.partner_runtime import PartnerRuntimeSettings


PATH = Path(__file__).resolve().parents[1] / "ops/partner-platform/prepare_private_manager_config.py"
SPEC = importlib.util.spec_from_file_location("private_manager_transition", PATH)
transition = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transition)

TOKEN = "123456789:" + "T" * 35
PASSWORD = "artificial-db-password-16:@/ +%"


class PrivateManagerConfigTests(unittest.TestCase):
    def config(self):
        return build_config(owner_id=transition.OWNER_ID, database_password=PASSWORD,
                            manager_token=TOKEN)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.private = self.folder / "private"
        self.private.mkdir(mode=0o700)
        self.config_path = self.private / "staging.json"
        self.backup_path = self.private / "staging.before-manager-enable.json"
        self.ca_folder = self.folder / "ca"
        self.ca_folder.mkdir(mode=0o755)
        self.ca_path = self.ca_folder / "supabase.crt"
        self.ca_bytes = b"-----BEGIN CERTIFICATE-----\nARTIFICIAL\n-----END CERTIFICATE-----\n"
        self.ca_path.write_bytes(self.ca_bytes)
        self.ca_path.chmod(0o644)
        for key, value in {
            "CONFIG_PATH": self.config_path, "BACKUP_PATH": self.backup_path,
            "CA_PATH": self.ca_path, "CA_SHA256": hashlib.sha256(self.ca_bytes).hexdigest(),
            "ROOT_UID": os.geteuid(),
        }.items():
            patcher = patch.object(transition, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_config(self, config=None):
        raw = json.dumps(config or self.config(), ensure_ascii=False, indent=2).encode() + b"\n"
        self.config_path.write_bytes(raw)
        self.config_path.chmod(0o600)
        return raw

    def test_pure_transition_preserves_every_credential_and_input_mapping(self):
        config = self.config()
        original = deepcopy(config)
        result = transition.make_transition(config, PartnerRuntimeSettings)
        self.assertEqual(config, original)
        self.assertIsNot(result, config)
        runtime = result["runtime"]
        for key in ("database_dsn", "manager_bot_token", "encryption_keys",
                    "webhook_key_hex", "subscription_key_hex"):
            self.assertEqual(runtime[key], original["runtime"][key])
        self.assertEqual(runtime["mode"], "staging")
        self.assertEqual(runtime["database_ca_file"], str(self.ca_path))
        self.assertTrue(runtime["database_tls"])
        self.assertEqual(runtime["manager_allowed_user_ids"], [transition.OWNER_ID])
        self.assertEqual(runtime["manager_origin"], "https://partners.wayspn.online")
        self.assertEqual(runtime["webhook_origin"], "https://partners.wayspn.online")
        for key in ("staging_isolation_confirmed", "database_isolation_confirmed",
                    "database_api_disabled_confirmed", "allow_staging_network",
                    "allow_staging_writes", "enable_http", "enable_manager_webhook"):
            self.assertIs(runtime[key], True, key)
        settings = PartnerRuntimeSettings.from_mapping(runtime)
        self.assertTrue(settings.preflight()["configured"])
        for key in ("enable_billing", "invoice_intents_enabled", "panel_identity_enabled",
                    "enable_provisioning", "enable_launches", "enable_customer_webhooks",
                    "enable_notifications", "enable_marketing_delivery", "enable_support_delivery",
                    "enable_finance_notifications", "enable_public_gateway", "enable_public_checkout",
                    "enable_worker", "management_enabled", "finance_enabled", "branding_enabled"):
            self.assertIs(getattr(settings, key), False, key)

    def test_wrong_owner_project_and_existing_payment_or_activation_state_refused(self):
        changes = (
            {"manager_allowed_user_ids": [transition.OWNER_ID + 1]},
            {"manager_allowed_user_ids": [transition.OWNER_ID, 42]},
            {"database_project_ref": "wrongprojectref"},
            {"mode": "staging"}, {"allow_staging_network": True},
            {"enable_http": True}, {"enable_billing": True},
            {"invoice_intents_enabled": True}, {"panel_identity_enabled": True},
            {"enable_provisioning": True}, {"enable_public_checkout": True},
            {"finance_enabled": True}, {"yookassa_secret_key": "existing-merchant-secret"},
            {"remnawave_api_token": "existing-panel-secret"},
        )
        for change in changes:
            config = self.config()
            config["runtime"].update(change)
            with self.subTest(fields=list(change)), self.assertRaises(Exception) as error:
                transition.make_transition(config, PartnerRuntimeSettings)
            for secret in (TOKEN, PASSWORD, "existing-merchant-secret", "existing-panel-secret"):
                self.assertNotIn(secret, str(error.exception))

    def test_apply_preserves_original_private_backup_and_atomically_writes_new_config(self):
        original = self.write_config()
        transition.apply_private_config(PartnerRuntimeSettings)
        self.assertEqual(self.backup_path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.backup_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(self.config_path.stat().st_uid, os.geteuid())
        self.assertEqual(self.config_path.stat().st_nlink, 1)
        result = json.loads(self.config_path.read_bytes())
        self.assertEqual(result["runtime"]["mode"], "staging")
        before = json.loads(original)["runtime"]
        for key in ("database_dsn", "manager_bot_token", "encryption_keys",
                    "webhook_key_hex", "subscription_key_hex"):
            self.assertEqual(result["runtime"][key], before[key])
        self.assertEqual(sorted(path.name for path in self.private.iterdir()),
                         sorted([self.config_path.name, self.backup_path.name]))
        current = self.config_path.read_bytes()
        with self.assertRaises(Exception):
            transition.apply_private_config(PartnerRuntimeSettings)
        self.assertEqual(self.config_path.read_bytes(), current)
        self.assertEqual(self.backup_path.read_bytes(), original)

    def test_existing_backup_is_never_overwritten(self):
        original = self.write_config()
        self.backup_path.write_bytes(b"pre-existing-backup")
        self.backup_path.chmod(0o600)
        with self.assertRaises(Exception):
            transition.apply_private_config(PartnerRuntimeSettings)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(self.backup_path.read_bytes(), b"pre-existing-backup")

    def test_temporary_name_collision_never_removes_existing_file(self):
        original = self.write_config()
        collision = self.private / ".manager-config-fixed.tmp"
        collision.write_bytes(b"pre-existing-unrelated-file")
        with patch.object(transition.secrets, "token_hex", return_value="fixed"):
            with self.assertRaises(Exception):
                transition.apply_private_config(PartnerRuntimeSettings)
        self.assertEqual(collision.read_bytes(), b"pre-existing-unrelated-file")
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(self.backup_path.read_bytes(), original)

    def test_bad_config_or_parent_modes_and_hardlinks_refused_without_backup(self):
        original = self.write_config()
        for path, mode in ((self.config_path, 0o640), (self.private, 0o750)):
            previous = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode)
            with self.subTest(path=path.name), self.assertRaises(Exception):
                transition.apply_private_config(PartnerRuntimeSettings)
            path.chmod(previous)
            self.assertEqual(self.config_path.read_bytes(), original)
            self.assertFalse(self.backup_path.exists())
        hardlink = self.private / "other.json"
        os.link(self.config_path, hardlink)
        with self.assertRaises(Exception):
            transition.apply_private_config(PartnerRuntimeSettings)
        self.assertFalse(self.backup_path.exists())

    def test_file_and_parent_symlinks_refused_without_mutation(self):
        original = self.write_config()
        real = self.private / "real.json"
        self.config_path.rename(real)
        self.config_path.symlink_to(real)
        with self.assertRaises(Exception):
            transition.apply_private_config(PartnerRuntimeSettings)
        self.assertEqual(real.read_bytes(), original)
        self.assertFalse(self.backup_path.exists())
        self.config_path.unlink()
        real.rename(self.config_path)
        linked_parent = self.folder / "linked-private"
        linked_parent.symlink_to(self.private, target_is_directory=True)
        with patch.object(transition, "CONFIG_PATH", linked_parent / self.config_path.name), patch.object(
            transition, "BACKUP_PATH", linked_parent / self.backup_path.name
        ), self.assertRaises(Exception):
            transition.apply_private_config(PartnerRuntimeSettings)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertFalse(self.backup_path.exists())

    def test_wrong_or_unsafe_ca_refused_before_config_mutation(self):
        original = self.write_config()
        self.ca_path.write_bytes(self.ca_bytes + b"changed")
        with self.assertRaises(Exception):
            transition.apply_private_config(PartnerRuntimeSettings)
        self.ca_path.write_bytes(self.ca_bytes)
        for path, mode in ((self.ca_path, 0o666), (self.ca_folder, 0o777)):
            previous = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode)
            with self.subTest(path=path.name), self.assertRaises(Exception):
                transition.apply_private_config(PartnerRuntimeSettings)
            path.chmod(previous)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertFalse(self.backup_path.exists())

    def test_default_plan_does_not_open_config_ca_or_runtime(self):
        output = io.StringIO()
        with patch.object(transition, "apply_private_config") as apply, patch(
            "builtins.open", side_effect=AssertionError("no reads")
        ), patch("io.open", side_effect=AssertionError("no reads")), patch(
            "os.open", side_effect=AssertionError("no reads")
        ), contextlib.redirect_stdout(output):
            self.assertEqual(transition.main([]), 0)
        apply.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])


if __name__ == "__main__":
    unittest.main()
