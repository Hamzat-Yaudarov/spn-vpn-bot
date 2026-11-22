import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "five_monitor_install", ROOT / "ops/ip-rotation-five/install_ro_monitor.py"
)
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class FiveMonitorScopeTests(unittest.TestCase):
    def test_only_exact_selected_five_are_measured_and_purchase_is_not_enabled(self):
        refresh, selected, billing = INSTALL.configs("https://panel.example.invalid")
        wanted = {
            "7737ba07-6d54-435c-8c01-aea4fe1369b9",
            "8a26ad46-2a93-49c0-995d-81014fbe8fce",
            "1db3bc12-a131-45da-8100-85344ede076d",
            "f07a16c5-5111-49d0-9801-f728135aa528",
            "c8274c47-23a4-47bb-b8a4-f3e5dcbac609",
        }
        self.assertEqual(set(refresh["selected_host_uuids"]), wanted)
        self.assertFalse(selected["auto_purchase_enabled"])
        self.assertTrue(selected["bypasses_excluded"])
        self.assertFalse(wanted & set(selected["protected_host_uuids"]))
        self.assertEqual(len({row["node_uuid"] for row in INSTALL.SELECTED}), 5)
        self.assertEqual(len(billing["servers"]), 5)
        self.assertEqual(billing["budget"]["max_daily_rub"], "3000.00")
        self.assertEqual(billing["budget"]["timezone"], "Europe/Saratov")
        self.assertTrue(all(row["max_monthly_rub"] == "300.00" for row in billing["servers"].values()))
        self.assertFalse(selected["refunds_replenish_daily_budget"])

    def test_changed_finnish_ips_do_not_swap_host_node_or_service(self):
        rows = {row["host_uuid"]: row for row in INSTALL.SELECTED}
        first = rows["1db3bc12-a131-45da-8100-85344ede076d"]
        second = rows["f07a16c5-5111-49d0-9801-f728135aa528"]
        self.assertEqual((first["ip_at_enrollment"], first["service_id"]),
                         ("87.251.16.245", "9b0316d5-fa83-4bf8-b6dc-8202b66622c9"))
        self.assertEqual((second["ip_at_enrollment"], second["service_id"]),
                         ("87.251.16.241", "626633f5-3bf3-48c7-aaba-9bbf43f8ea9d"))
        self.assertNotIn("dns_names", second)


if __name__ == "__main__":
    unittest.main()
