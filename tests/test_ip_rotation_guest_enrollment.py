import base64
from contextlib import nullcontext
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from spn_ip_failover import managed_guest


SCRIPT = Path(__file__).resolve().parents[1] / "ops/ip-rotation-five/guest_enrollment.py"
spec = importlib.util.spec_from_file_location("guest_enrollment", SCRIPT)
enroll = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = enroll
spec.loader.exec_module(enroll)

SWEDEN = "f5c0287a-636e-4981-95c7-80b24c9559c0"
ESTONIA = "21e508f0-2ef2-47da-aee7-15dc8c66eb15"
IP = "8.8.8.8"
OLD_IP = "8.8.4.4"
SOURCE = b"# known pinned helper\n"
SHA = enroll.digest(SOURCE)
RAW = ("auto lo\niface lo inet loopback\n"
       "auto ens3\niface ens3 inet static\n    address 8.8.8.8/32\n    gateway 10.0.0.1\n"
       "iface ens3 inet6 static\n    address 2001:4860:4860::8888/64\n").encode()


def recorded(raw, mode=0o600, *, content=True):
    value = {"uid": 0, "gid": 0, "mode": mode, "size": len(raw), "sha256": enroll.digest(raw)}
    if content:
        value["base64"] = base64.b64encode(raw).decode()
    return value


def snapshot(service=ESTONIA, legacy_ip=IP):
    deployment = enroll.SERVICES[service]
    before = {"dst": "default", "gateway": "10.0.0.1", "dev": "ens3", "flags": ["onlink"]}
    legacy = None
    units = ""
    if deployment:
        plan = {"version": 1, "deployment": deployment, "target": legacy_ip,
                "before": before, "after": {**before, "prefsrc": legacy_ip}, "beforeSource": legacy_ip,
                "helperHash": SHA}
        plan_record = recorded(enroll.encoded(plan))
        legacy = {"unit": recorded(enroll.unit_bytes(deployment, legacy_ip), mode=0o644),
                  "plan": plan_record, "source": recorded(SOURCE, content=False),
                  "stage_plan": copy.deepcopy(plan_record), "active_state": "active"}
        units = "spn-reality-source-" + deployment + ".service enabled enabled"
    return {"format": 1, "service_id": service, "ip": IP, "deployment": deployment, "interface": "ens3",
            "network": {"addresses": [{"ifname": "ens3", "flags": ["UP"],
                                        "addr_info": [{"family": "inet", "local": IP, "prefixlen": 32}]}],
                        "rules": [{"priority": p, "src": "all", "table": t}
                                  for p, t in ((0, "local"), (32766, "main"), (32767, "default"))],
                        "default": [{**before, "prefsrc": IP}],
                        "selected": [{"dev": "ens3", "gateway": "10.0.0.1", "prefsrc": IP}]},
            "managers": {"networking": "active", "NetworkManager": "inactive",
                         "systemd-networkd": "inactive", "ifquery": "ens3=ens3"},
            "interfaces": recorded(RAW, mode=0o644), "network_files": [], "units": units, "legacy": legacy}


class EnrollmentPlanTests(unittest.TestCase):
    def validate(self, value, *, legacy_ip=None, service=None):
        return enroll.validate_snapshot(value, service or value["service_id"], IP, SHA, managed_guest,
                                        legacy_ip=legacy_ip)

    def test_plan_is_pure_and_preserves_ipv6(self):
        with patch.object(enroll.subprocess, "run") as command, patch.object(managed_guest, "atomic_write") as write:
            settings, raw = self.validate(snapshot())
            self.assertEqual(settings.bootstrap["active"], {"id": IP, "ip": IP})
            self.assertIn(b"iface ens3 inet6 static\n    address 2001:4860:4860::8888/64\n", raw)
            self.assertIn(b"post-up /usr/sbin/ip -4 route replace default via 10.0.0.1 dev ens3 onlink src 8.8.8.8", raw)
            command.assert_not_called()
            write.assert_not_called()

    def test_manual_installation_needs_no_legacy_source_unit(self):
        settings, _ = self.validate(snapshot(SWEDEN))
        self.assertEqual(settings.server_id, SWEDEN)
        value = snapshot(SWEDEN)
        value["units"] = "spn-reality-source-" + "a" * 32 + ".service disabled enabled"
        with self.assertRaisesRegex(ValueError, "Unexpected preferred-source"):
            self.validate(value)

    def test_diagnostic_missing_interfaces_or_ifquery_can_never_enroll(self):
        for field in ("interfaces", "missing", "unavailable"):
            value = snapshot()
            if field == "interfaces":
                value["interfaces"] = None
                message = "interfaces file is missing"
            else:
                value["managers"]["ifquery"] = field
                message = "ifquery is missing/unavailable"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                self.validate(value)

    def test_optional_diagnostic_probe_reports_missing_or_failure_without_command_details(self):
        with patch.object(enroll.Path, "is_file", return_value=False), patch.object(enroll, "run") as command:
            self.assertEqual(enroll.optional_probe("/usr/sbin/ifquery", "--state", "ens3"), "missing")
            command.assert_not_called()
        with patch.object(enroll.Path, "is_file", return_value=True), \
                patch.object(enroll, "run", side_effect=ValueError("secret-like stderr must not leak")):
            self.assertEqual(enroll.optional_probe("/usr/sbin/ifquery", "--state", "ens3"), "unavailable")

    def test_capture_remains_diagnostic_when_ifupdown_is_absent(self):
        expected = snapshot(SWEDEN)
        network = expected["network"]

        def command(*args, **_kwargs):
            if args[0] == "/usr/sbin/ip":
                names = {("address", "show", "scope", "global"): "addresses", ("rule", "show"): "rules",
                         ("route", "show", "table", "main", "default"): "default",
                         ("route", "get", "1.1.1.1"): "selected"}
                return json.dumps(network[names[args[3:]]])
            if args[1] == "is-active":
                return "active" if args[2] == "systemd-networkd" else "inactive"
            if args[1] == "list-unit-files":
                return ""
            raise AssertionError("Unexpected command")

        with patch.object(enroll, "run", side_effect=command), patch.object(enroll, "record", return_value=None), \
                patch.object(enroll, "optional_probe", return_value="missing"), \
                patch.object(enroll, "network_file_metadata", return_value=[{"path": "/etc/netplan/50-cloud-init.yaml", "sha256": "a" * 64}]):
            value = enroll.capture(SWEDEN, IP)
        self.assertIsNone(value["interfaces"])
        self.assertEqual(value["managers"]["ifquery"], "missing")
        self.assertEqual(value["managers"]["networkctl"], "missing")
        self.assertEqual(value["network_files"][0]["path"], "/etc/netplan/50-cloud-init.yaml")

    def test_netplan_diagnostic_never_collects_yaml_contents(self):
        path = Path("/etc/netplan/50-cloud-init.yaml")
        metadata = recorded(b"network: {password: must-not-be-emitted}\n", content=False)
        with patch.object(enroll.Path, "glob", side_effect=lambda self_pattern: []), \
                patch.object(enroll, "record", return_value=metadata) as read:
            # All directories intentionally return no files: no extra scans.
            self.assertEqual(enroll.network_file_metadata(), [])
            read.assert_not_called()
        with patch.object(enroll.Path, "glob", return_value=[path]), \
                patch.object(enroll, "record", return_value=metadata) as read:
            value = enroll.network_file_metadata()
        self.assertTrue(all(call.kwargs == {"content": False} for call in read.call_args_list))
        self.assertNotIn("base64", json.dumps(value))
        self.assertNotIn("must-not-be-emitted", json.dumps(value))

    def test_old_ip_requires_explicit_pin_and_does_not_have_to_remain_assigned(self):
        value = snapshot(legacy_ip=OLD_IP)
        with self.assertRaisesRegex(ValueError, "previous deployment IP"):
            self.validate(value)
        settings, _ = self.validate(value, legacy_ip=OLD_IP)
        self.assertEqual(settings.bootstrap["expected_live_ipv4"], [IP])
        with self.assertRaisesRegex(ValueError, "previous deployment IP"):
            self.validate(value, legacy_ip="9.9.9.9")

    def test_expected_deployment_and_template_are_not_inferred_from_remote_claim(self):
        value = snapshot()
        value["deployment"] = "b" * 32
        with self.assertRaisesRegex(ValueError, "different VPS"):
            self.validate(value)
        value = snapshot()
        value["legacy"]["unit"] = recorded(enroll.unit_bytes(value["deployment"], IP).replace(b"User=root", b"User=other"), 0o644)
        with self.assertRaisesRegex(ValueError, "Legacy unit differs"):
            self.validate(value)

    def test_unknown_or_transitional_legacy_units_are_rejected(self):
        for change in (lambda v: v.update(units=v["units"] + "\nother.service enabled enabled"),
                       lambda v: v.update(units=v["units"].replace(" enabled enabled", " static -")),
                       lambda v: v["legacy"].update(active_state="activating")):
            value = snapshot()
            change(value)
            with self.assertRaises(ValueError):
                self.validate(value)

    def test_helper_code_digest_owner_and_stage_plan_must_match(self):
        for change in (lambda v: v["legacy"]["source"].update(sha256="a" * 64),
                       lambda v: v["legacy"]["source"].update(uid=1000),
                       lambda v: v["legacy"]["source"].update(mode=0o666),
                       lambda v: v["legacy"].update(stage_plan=recorded(b"{}"))):
            value = snapshot()
            change(value)
            with self.assertRaises(ValueError):
                self.validate(value)

    def test_manager_route_source_prefix_and_extra_addresses_are_checked(self):
        for change in (lambda v: v["network"]["default"][0].update(gateway="10.0.0.2"),
                       lambda v: v["network"]["selected"][0].update(prefsrc=OLD_IP),
                       lambda v: v["network"]["addresses"][0]["addr_info"][0].update(prefixlen=24),
                       lambda v: v["network"]["addresses"][0]["addr_info"].append({"family": "inet", "local": OLD_IP, "prefixlen": 32}),
                       lambda v: v["managers"].update(NetworkManager="active"),
                       lambda v: v["interfaces"].update(sha256="0" * 64)):
            value = snapshot()
            change(value)
            with self.assertRaises(ValueError):
                self.validate(value)

    def test_gateway_device_change_from_old_approved_plan_is_not_ip_handoff(self):
        value = snapshot(legacy_ip=OLD_IP)
        plan = json.loads(enroll.body(value["legacy"]["plan"]))
        plan["before"]["dev"] = "eth7"
        plan["after"]["dev"] = "eth7"
        value["legacy"]["plan"] = recorded(enroll.encoded(plan))
        value["legacy"]["stage_plan"] = recorded(enroll.encoded(plan))
        with self.assertRaisesRegex(ValueError, "apart from the authorized source IP"):
            self.validate(value, legacy_ip=OLD_IP)


class EnrollmentApplyTests(unittest.TestCase):
    def run_apply(self, value, *, current=None, guest_error=None):
        settings, rendered = enroll.validate_snapshot(value, ESTONIA, IP, SHA, managed_guest)
        events = []
        guest = Mock()
        if guest_error:
            guest.run.side_effect = guest_error
        else:
            guest.run.return_value = {"ok": True}
        store = Mock()
        store.locked.return_value = nullcontext()
        managed = SimpleNamespace(FileStore=Mock(return_value=store), ManagedGuest=Mock(return_value=guest),
                                  SystemKernel=Mock())

        def command(*args, **_kwargs):
            events.append(("command", args))
            return "disabled" if args[1] == "is-enabled" else ""

        def write(path, raw, _managed):
            events.append(("write", path.name, raw))

        meta = SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1)
        with patch.object(enroll, "create_directory"), patch.object(enroll.os, "open", return_value=99), \
                patch.object(enroll.os, "fstat", return_value=meta), patch.object(enroll.os, "close"), \
                patch.object(enroll.fcntl, "flock"), patch.object(enroll, "write_once", side_effect=write), \
                patch.object(enroll, "capture", return_value=current or copy.deepcopy(value)), \
                patch.object(enroll, "record", return_value=recorded(SOURCE)), \
                patch.object(enroll, "run", side_effect=command):
            try:
                result = enroll.apply(value, settings, rendered, SHA, managed)
                return result, events, guest
            except Exception as exc:
                return exc, events, guest

    def test_durable_backups_precede_only_exact_unit_disable_no_stop_or_restart(self):
        result, events, guest = self.run_apply(snapshot())
        self.assertTrue(result["ready"])
        self.assertFalse(result["network_restarted"])
        self.assertFalse(result["vpn_restarted"])
        commands = [row[1] for row in events if row[0] == "command"]
        unit = "spn-reality-source-" + enroll.SERVICES[ESTONIA] + ".service"
        self.assertEqual(commands, [("/usr/bin/systemctl", "disable", unit),
                                    ("/usr/bin/systemctl", "is-enabled", unit)])
        before_disable = events[:next(i for i, row in enumerate(events) if row[0] == "command")]
        self.assertTrue({"snapshot.json", "intent.json", "legacy-unit", "legacy-plan", "legacy-stage_plan",
                         "legacy-source.py", "legacy-source-metadata.json"}
                        <= {row[1] for row in before_disable if row[0] == "write"})
        self.assertEqual([call.args[0]["operation"] for call in guest.run.call_args_list], ["bootstrap", "check"])
        self.assertEqual(events[-1][1], "complete.json")

    def test_already_disabled_owned_unit_is_not_toggled_or_stopped(self):
        current = snapshot()
        current["units"] = current["units"].replace(" enabled enabled", " disabled enabled")
        result, events, _ = self.run_apply(snapshot(), current=current)
        self.assertTrue(result["ready"])
        self.assertEqual([row[1][1] for row in events if row[0] == "command"], ["is-enabled"])

    def test_live_drift_stops_before_unit_disable_or_guest_bootstrap(self):
        current = snapshot()
        current["network"]["selected"][0]["prefsrc"] = OLD_IP
        result, events, guest = self.run_apply(snapshot(), current=current)
        self.assertIsInstance(result, ValueError)
        self.assertEqual([row for row in events if row[0] == "command"], [])
        guest.run.assert_not_called()

    def test_bootstrap_failure_keeps_intent_and_backups_but_never_claims_complete(self):
        result, events, _ = self.run_apply(snapshot(), guest_error=ValueError("pending interrupted bootstrap"))
        self.assertIsInstance(result, ValueError)
        names = {row[1] for row in events if row[0] == "write"}
        self.assertIn("intent.json", names)
        self.assertIn("legacy-unit", names)
        self.assertNotIn("complete.json", names)


if __name__ == "__main__":
    unittest.main()
