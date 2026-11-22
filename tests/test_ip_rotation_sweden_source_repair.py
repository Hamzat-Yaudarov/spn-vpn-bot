import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
import unittest
from unittest.mock import Mock, patch

from spn_ip_failover import netplan_guest as netplan

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sweden_source_repair_test", ROOT / "ops/ip-rotation-five/repair_sweden_source.py")
repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repair)
MAC = "52:54:00:6b:3d:1b"
RAW = b'''network:
  version: 2
  ethernets:
    eth0:
      addresses:
      - 177.3.250.179/32
      - 2a12:bec4:1484:245::2/64
      dhcp4: false
      dhcp6: true
      match:
        macaddress: 52:54:00:6b:3d:1b
      set-name: eth0
      nameservers:
        addresses: [1.1.1.1, '2606:4700:4700::1111']
      routes:
      - to: default
        via: 10.0.0.1
        on-link: true
        from: 177.3.250.133 # owned stale source only
      - to: default
        via: 2a12:bec4:1484:245::1
        on-link: true
'''


def status():
    return {"Name": "eth0", "Index": 2, "Type": "ether", "AdministrativeState": "configuring",
            "OperationalState": "routable", "IPv4AddressState": "routable", "OnlineState": "online",
            "NetworkFile": str(repair.NETWORK), "NetworkFileDropins": [],
            "HardwareAddress": [82, 84, 0, 107, 61, 27],
            "Addresses": [{"Family": 2, "Address": [177, 3, 250, 179], "PrefixLength": 32,
                           "ConfigSource": "static", "ConfigState": "configured", "Scope": 0}],
            "Routes": [{"Family": 2, "ConfigSource": "foreign", "ConfigState": "configured"},
                       {"Family": 2, "Destination": [0, 0, 0, 0], "DestinationPrefixLength": 0,
                        "Gateway": [10, 0, 0, 1], "PreferredSource": [177, 3, 250, 133],
                        "Table": 254, "Type": 1, "ConfigSource": "static", "ConfigState": "requesting"}]}


def diagnostic():
    return {"interface": "eth0", "interfaces": None,
            "managers": {"systemd-networkd": "active", "NetworkManager": "inactive", "networking": "inactive"},
            "network": {"rules": [{"priority": p, "src": "all", "table": t}
                                  for p, t in ((0, "local"), (32766, "main"), (32767, "default"))],
                        "addresses": [{"ifname": "eth0", "flags": ["UP"],
                                       "addr_info": [{"family": "inet", "local": repair.NEW, "prefixlen": 32}]}],
                        "default": [{"dst": "default", "gateway": "10.0.0.1", "dev": "eth0", "flags": ["onlink"]}],
                        "selected": [{"dev": "eth0", "gateway": "10.0.0.1", "prefsrc": repair.NEW}]}}


class SourceRepairPureTests(unittest.TestCase):
    def test_only_one_source_line_changes_other_bytes_ipv6_dns_and_comments_preserved(self):
        result = repair.render_source(RAW, netplan)
        self.assertEqual(result, RAW.replace(b"from: 177.3.250.133", b"from: 177.3.250.179"))
        before, after = netplan.parse_yaml(RAW), netplan.parse_yaml(result)
        before["network"]["ethernets"]["eth0"]["routes"][0]["from"] = repair.NEW
        self.assertEqual(after, before)

    def test_quoted_standalone_source_retains_quotes(self):
        for quote in (b"'", b'"'):
            raw = RAW.replace(b"from: 177.3.250.133", b"from: " + quote + b"177.3.250.133" + quote)
            self.assertIn(b"from: " + quote + b"177.3.250.179" + quote, repair.render_source(raw, netplan))

    def test_dynamic_extra_wrong_or_already_changed_source_is_refused(self):
        for raw in (RAW.replace(b"dhcp4: false", b"dhcp4: true"),
                    RAW.replace(b"- 177.3.250.179/32", b"- 177.3.250.179/32\n      - 8.8.8.8/32"),
                    RAW.replace(b"from: 177.3.250.133", b"from: 8.8.8.8"),
                    RAW.replace(b"from: 177.3.250.133", b"from: 177.3.250.179")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                repair.render_source(raw, netplan)

    def test_exact_observed_pending_ipv4_request_is_supported_only_for_planning(self):
        repair.pending_old_source(status(), MAC)
        # This predicate never labels the interface ready/configured.
        self.assertEqual(status()["AdministrativeState"], "configuring")

    def test_every_ambiguous_networkd_identity_or_pending_route_is_refused(self):
        for field, value in (("Name", "eth1"), ("Index", True), ("Type", "bridge"),
                             ("AdministrativeState", "failed"), ("IPv4AddressState", "degraded"),
                             ("NetworkFile", "/run/foreign.network"), ("NetworkFileDropins", ["override.conf"]),
                             ("HardwareAddress", [0] * 6), ("OnlineState", "offline")):
            row = status(); row[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                repair.pending_old_source(row, MAC)
        for field, value in (("ConfigState", "configuring"), ("ConfigSource", "dhcp4"),
                             ("PreferredSource", [177, 3, 250, 179]), ("Gateway", [10, 0, 0, 2]),
                             ("DestinationPrefixLength", 24), ("Table", 100), ("Type", 2)):
            row = status(); row["Routes"][-1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                repair.pending_old_source(row, MAC)
        row = status(); row["Routes"].append(copy.deepcopy(row["Routes"][-1]))
        with self.assertRaises(ValueError): repair.pending_old_source(row, MAC)

    def test_non_static_or_duplicate_ipv4_address_is_refused(self):
        for field, value in (("ConfigSource", "dhcp4"), ("ConfigState", "requesting"),
                             ("PrefixLength", 24), ("Address", [177, 3, 250, 133]), ("Scope", 253)):
            row = status(); row["Addresses"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                repair.pending_old_source(row, MAC)
        row = status(); row["Addresses"].append(copy.deepcopy(row["Addresses"][0]))
        with self.assertRaises(ValueError): repair.pending_old_source(row, MAC)

    def test_working_foreign_boot_route_and_exact_static_route_are_read_only_accepted(self):
        row = diagnostic(); repair.live_ipv4(row)
        row["network"]["default"][0].update(protocol="static", prefsrc=repair.NEW)
        repair.live_ipv4(row)

    def test_live_extra_ip_wrong_source_gateway_rule_or_manager_is_refused(self):
        changes = [lambda v: v["network"]["addresses"][0]["addr_info"].append({"family": "inet", "local": "8.8.8.8", "prefixlen": 32}),
                   lambda v: v["network"]["default"][0].update(prefsrc=repair.OLD),
                   lambda v: v["network"]["default"][0].update(gateway="10.0.0.2"),
                   lambda v: v["network"]["selected"][0].update(prefsrc=repair.OLD),
                   lambda v: v["network"]["rules"].append({"priority": 1000, "src": "all", "table": 100}),
                   lambda v: v["managers"].update(NetworkManager="active")]
        for change in changes:
            row = diagnostic(); change(row)
            with self.assertRaises(ValueError): repair.live_ipv4(row)

    def test_postcheck_uses_existing_strict_manager_and_static_route_guards(self):
        helper = SimpleNamespace(record=Mock(), body=Mock(side_effect=[b"yaml", b"network"]), capture=Mock(return_value="diagnostic"))
        kernel = SimpleNamespace(manager=Mock())
        n = SimpleNamespace(SystemKernel=Mock(return_value=kernel))
        e = SimpleNamespace(network_proof=Mock())
        m = SimpleNamespace(egress=Mock(return_value=repair.NEW))
        plan = {"settings": SimpleNamespace(netplan_guard={"macaddress": MAC}), "yaml": b"yaml", "generated": b"network"}
        row = status(); row["AdministrativeState"] = "configured"; row["Routes"][-1]["ConfigState"] = "configured"
        with patch.object(repair, "network_json", return_value=row): repair.postcheck(plan, (e, helper, None, n, m))
        kernel.manager.assert_called_once_with()
        e.network_proof.assert_called_once_with("diagnostic", repair.NEW)
        kernel.manager.side_effect = RuntimeError("still configuring")
        with patch.object(repair, "network_json", return_value=row), self.assertRaises(RuntimeError):
            helper.body.side_effect = [b"yaml", b"network"]
            repair.postcheck(plan, (e, helper, None, n, m))

    def test_default_cli_performs_no_network_and_requires_separate_reconfigure_permission(self):
        with patch.object(repair.sys, "argv", ["repair_sweden_source.py"]), \
                patch.object(repair, "load_runtime") as load, patch("builtins.print") as emit:
            repair.main()
        load.assert_not_called()
        value = json.loads(emit.call_args.args[0])
        self.assertTrue(value["apply_requires_separate_reconfigure_permission"])
        self.assertTrue(value["can_interrupt_network"])
        self.assertFalse(value["network_used"])

    def test_apply_freezes_backup_arms_independent_watchdog_then_writes_and_strict_checks_before_complete(self):
        events = []
        snap = {"snapshot": {"current": {"netplan": "before-yaml", "generated": {"mode": 0o640, "gid": 0}}}}
        helper = SimpleNamespace(record=Mock(),
                                 write_once=lambda path, *_: events.append("freeze:" + path.name))
        managed = SimpleNamespace(atomic_write=lambda path, *_a, **_k: events.append("write:" + path.name))
        n = SimpleNamespace(FileStore=Mock(return_value=SimpleNamespace(locked=lambda: nullcontext())))
        m = SimpleNamespace(directories=lambda *_: None,
                            validate_agreement=lambda *_: events.append("financial-proof"))
        plan = {"epoch": "a" * 64, "settings": "fresh", "old_settings": "old", "yaml": b"newyaml", "generated": b"newnetwork"}
        def run(argv, **_):
            events.append(tuple(argv))
            return ""
        with patch.object(repair, "old_journals_unchanged", side_effect=lambda *_: events.append("old-preserved")), \
                patch.object(repair, "capture", return_value=snap), patch.object(repair, "command", side_effect=run), \
                patch.object(repair, "postcheck", side_effect=lambda *_: events.append("strict-postcheck")):
            result = repair.apply(snap, {}, plan, (None, helper, managed, n, m))
        arm = next(i for i, e in enumerate(events) if isinstance(e, tuple) and e[0] == "/usr/bin/systemd-run")
        first_write = events.index("write:50-cloud-init.yaml")
        self.assertLess(events.index("freeze:snapshot.json"), arm)
        self.assertLess(events.index("freeze:candidate.network"), arm)
        self.assertLess(arm, first_write)
        self.assertIn(("/usr/bin/networkctl", "reconfigure", "eth0"), events)
        self.assertLess(events.index("strict-postcheck"), events.index("freeze:complete.json"))
        self.assertFalse(result["epoch_published"])
        self.assertFalse(any("restart" in str(e) for e in events))

    def test_watchdog_arm_failure_prevents_all_network_file_writes(self):
        helper = SimpleNamespace(record=Mock(), write_once=Mock())
        managed = SimpleNamespace(atomic_write=Mock())
        n = SimpleNamespace(FileStore=Mock(return_value=SimpleNamespace(locked=lambda: nullcontext())))
        m = SimpleNamespace(directories=Mock(), validate_agreement=Mock())
        plan = {"epoch": "a" * 64, "old_settings": "old", "yaml": b"newyaml", "generated": b"newnetwork"}
        with patch.object(repair, "old_journals_unchanged"), patch.object(repair, "capture", return_value={}), \
                patch.object(repair, "command", side_effect=RuntimeError("watchdog not armed")), \
                patch.object(repair, "watchdog") as rollback:
            with self.assertRaises(RuntimeError): repair.apply({}, {}, plan, (None, helper, managed, n, m))
        managed.atomic_write.assert_not_called()
        rollback.assert_not_called()

    def test_failed_reconfigure_uses_working_route_watchdog_never_publishes_epoch_or_completion(self):
        frozen = []
        helper = SimpleNamespace(record=Mock(), write_once=lambda path, *_: frozen.append(path.name))
        managed = SimpleNamespace(atomic_write=Mock())
        n = SimpleNamespace(FileStore=Mock(return_value=SimpleNamespace(locked=lambda: nullcontext())))
        m = SimpleNamespace(directories=Mock(), validate_agreement=Mock())
        snap = {"snapshot": {"current": {"netplan": "before-yaml", "generated": {"mode": 0o640, "gid": 0}}}}
        plan = {"epoch": "a" * 64, "old_settings": "old", "yaml": b"newyaml", "generated": b"newnetwork"}
        def run(argv, **_):
            if argv[:2] == ["/usr/bin/networkctl", "reconfigure"]:
                raise RuntimeError("reconfigure failed")
            return ""
        with patch.object(repair, "old_journals_unchanged"), patch.object(repair, "capture", return_value=snap), \
                patch.object(repair, "command", side_effect=run), patch.object(repair, "watchdog") as rollback:
            with self.assertRaises(RuntimeError): repair.apply(snap, {}, plan, (None, helper, managed, n, m))
        rollback.assert_called_once()
        self.assertNotIn("complete.json", frozen)

    def test_watchdog_restores_only_working_new_address_route_and_refuses_foreign_ip(self):
        guard = {"macaddress": MAC, "networkd_inputs": {}, "cloud_init_file_sha256": "a" * 64}
        snap = {"snapshot": {"current": {"netplan_guard": guard, "netplan": "old-yaml", "generated": "old-network"}}}
        data = {"old-yaml": b"old-yaml", "old-network": b"old-network", "new-yaml": b"new-yaml", "new-network": b"new-network"}
        def record(path):
            return "new-yaml" if path.name in ("candidate.yaml", "50-cloud-init.yaml") else "new-network"
        helper = SimpleNamespace(record=record, body=lambda record, **_: data[record], write_once=Mock())
        n = SimpleNamespace(capture_guard=Mock(return_value=guard))
        commands = []
        address = repair.NEW
        def run(argv):
            commands.append(argv)
            if "address" in argv and "show" in argv:
                return json.dumps([{"flags": ["UP"], "addr_info": [{"family": "inet", "local": address, "prefixlen": 32}]}])
            if "route" in argv and "show" in argv:
                return "[]"
            return ""
        with patch.object(Path, "exists", return_value=False), patch.object(Path, "read_text", return_value=MAC), \
                patch.object(repair, "old_journals_unchanged"):
            repair.watchdog(snap, Path("/fake/archive"), (None, helper, None, n, None), run=run)
            self.assertIn(["/usr/sbin/ip", "address", "replace", repair.NEW + "/32", "dev", "eth0"], commands)
            self.assertIn(["/usr/sbin/ip", "route", "replace", "default", "via", "10.0.0.1", "dev", "eth0",
                           "proto", "static", "src", repair.NEW, "onlink"], commands)
            self.assertFalse(any(repair.OLD in " ".join(argv) for argv in commands))
            commands.clear(); address = "8.8.8.8"
            with self.assertRaises(ValueError):
                repair.watchdog(snap, Path("/fake/archive"), (None, helper, None, n, None), run=run)
            self.assertFalse(any("replace" in argv for argv in commands))


if __name__ == "__main__":
    unittest.main()
