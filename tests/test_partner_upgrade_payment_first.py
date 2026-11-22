"""Narrow payment-first release transition; artificial config only."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import partner_enable_owner_pilot as prepare
from services.partner_runtime import PartnerRuntimeSettings
from test_partner_enable_owner_pilot import source_config, SHOP, SECRET


BASE = Path(__file__).resolve().parents[1] / "ops/partner-platform"


def module(name):
    spec = importlib.util.spec_from_file_location("_payment_first_" + name, BASE / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


upgrade = module("upgrade_payment_first")
legacy = module("upgrade_private_pilot")


def previous():
    value = prepare.make_candidate(source_config(), shop_id=SHOP, secret_key=SECRET)
    value["runtime"].update(enable_owner_entry_pilot=False, enable_public_entry_trial=True,
                            manager_allowed_user_ids=None, enable_managed_bots=True,
                            enable_entry_launch_notifications=True)
    return json.dumps(value).encode()


class PaymentFirstUpgradeTests(unittest.TestCase):
    def test_default_is_pure_plan_without_even_loading_helpers(self):
        out = io.StringIO()
        with patch.object(upgrade, "load_ops") as load, patch.object(upgrade, "upgrade") as perform, \
             patch.object(upgrade, "backup_database") as backup, contextlib.redirect_stdout(out):
            self.assertEqual(upgrade.main([]), 0)
        load.assert_not_called(); perform.assert_not_called(); backup.assert_not_called()
        self.assertEqual(json.loads(out.getvalue())["entry_price_rub"], 9990)

    def test_legacy_validation_projection_is_memory_only_and_preserves_secrets(self):
        raw = previous()
        saved = upgrade.validate_previous(raw, lambda value: legacy.validate_config(value, features=True))
        self.assertEqual(saved, json.loads(raw))
        self.assertEqual(previous(), raw)
        for changes in ({"enable_public_entry_trial": False}, {"enable_owner_entry_pilot": True},
                        {"manager_allowed_user_ids": [upgrade.OWNER]}, {"enable_provisioning": True},
                        {"enable_public_entry_sales": True}, {"database_project_ref": "foreign"}):
            value = json.loads(raw)
            value["runtime"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(Exception):
                upgrade.validate_previous(json.dumps(value).encode(),
                    lambda value: legacy.validate_config(value, features=True))

    def test_candidate_changes_only_three_reviewed_flags_and_is_valid(self):
        before = json.loads(previous())
        after = json.loads(upgrade.candidate(previous()))
        changed = {name for name in set(before["runtime"]) | set(after["runtime"])
                   if before["runtime"].get(name) != after["runtime"].get(name)}
        self.assertEqual(changed, {"enable_public_entry_trial", "enable_public_entry_sales", "enable_payment_first_entry"})
        settings = PartnerRuntimeSettings.from_mapping(after["runtime"])
        self.assertTrue(settings.preflight()["configured"])
        self.assertTrue(settings.enable_public_entry_sales)
        self.assertFalse(settings.enable_provisioning)
        self.assertIsNone(settings.manager_allowed_user_ids)
        self.assertEqual(after["staging_addon_catalog"], before["staging_addon_catalog"])

    def test_failback_is_owner_only_and_never_reverts_financial_schema(self):
        fallback = json.loads(upgrade.closed_fallback(previous()))
        settings = PartnerRuntimeSettings.from_mapping(fallback["runtime"])
        self.assertTrue(settings.preflight()["configured"])
        self.assertEqual(settings.manager_allowed_user_ids, (upgrade.OWNER,))
        self.assertFalse(settings.enable_public_entry_trial)
        self.assertFalse(settings.enable_public_entry_sales)
        self.assertFalse(settings.enable_payment_first_entry)
        self.assertFalse(settings.enable_provisioning)
        source = (BASE / "upgrade_payment_first.py").read_text()
        self.assertNotIn("DROP TABLE", source)
        self.assertNotIn("ALTER TABLE", source)
        self.assertIn("ops.atomic(CONFIG, fallback)", source)

    def test_backup_and_target_are_fixed_and_separate_from_old_backup(self):
        self.assertEqual(upgrade.TARGET, "92.42.102.103")
        self.assertEqual(upgrade.OLD_RELEASE.name, "20261025-bb1171417098")
        self.assertEqual(upgrade.DATA_BACKUP.name, "before-payment-first-20261026.json")
        self.assertIn("ops/partner-platform/payment_first_entry.sql", upgrade.ALLOWED_CHANGED)


if __name__ == "__main__":
    unittest.main()
