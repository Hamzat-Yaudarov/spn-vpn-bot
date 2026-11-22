import base64
from contextlib import nullcontext
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from spn_ip_failover import managed_guest


ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


helper = module("netplan_enroll_test_util", ROOT / "ops/ip-rotation-five/guest_enrollment.py")
enroll = module("netplan_enroll_test", ROOT / "ops/ip-rotation-five/enroll_netplan.py")
ESTONIA = "21e508f0-2ef2-47da-aee7-15dc8c66eb15"
SWEDEN = "f5c0287a-636e-4981-95c7-80b24c9559c0"
IP = "8.8.8.8"
OLD = "8.8.4.4"
SOURCE = (ROOT / "ops/universal-reality/preferred_source.py").read_bytes()
RAW = b"network: {version: 2, ethernets: {eth0: {addresses: [8.8.8.8/32]}}}\n"
AFTER = RAW + b"# candidate only\n"
GENERATED = b"[Match]\nName=eth0\n[Network]\nAddress=8.8.8.8/32\n"
GENERATED_AFTER = GENERATED + b"[Route]\nPreferredSource=8.8.8.8\n"
CLOUD = b"network:\n  config: disabled\ndisable_network_activation: true\n"


def record(raw, mode=0o600, content=True):
    result = {"uid": 0, "gid": 0, "mode": mode, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if content:
        result["base64"] = base64.b64encode(raw).decode()
    return result


def diagnostic(service=ESTONIA):
    deployment = enroll.SERVICES[service]
    route = {"dst": "default", "dev": "eth0", "gateway": "10.0.0.1", "flags": ["onlink"], "protocol": "static"}
    legacy, units = None, ""
    if deployment:
        plan = {"version": 1, "deployment": deployment, "target": OLD, "before": route,
                "after": {**route, "prefsrc": OLD}, "beforeSource": OLD, "helperHash": enroll.SOURCE_SHA}
        plan_record = record(helper.encoded(plan))
        legacy = {"unit": record(helper.unit_bytes(deployment, OLD), 0o644), "plan": plan_record,
                  "stage_plan": copy.deepcopy(plan_record), "source": record(SOURCE, content=False),
                  "active_state": "active"}
        units = "spn-reality-source-" + deployment + ".service enabled enabled"
    return {"format": 1, "service_id": service, "ip": IP, "deployment": deployment, "interface": "eth0",
            "network": {"addresses": [{"ifname": "eth0", "flags": ["UP"],
                                        "addr_info": [{"family": "inet", "local": IP, "prefixlen": 32}]}],
                        "rules": [{"priority": p, "src": "all", "table": t}
                                  for p, t in ((0, "local"), (32766, "main"), (32767, "default"))],
                        "default": [route], "selected": [{"dev": "eth0", "gateway": "10.0.0.1", "prefsrc": IP,
                                                           "uid": 0, "cache": []}]},
            "managers": {"systemd-networkd": "active", "networking": "inactive", "NetworkManager": "inactive",
                         "ifquery": "missing", "networkctl": "2 eth0 ether routable configured"},
            "interfaces": None,
            "network_files": [{"path": str(enroll.YAML), **record(RAW, content=False)},
                              {"path": "/run/systemd/network/10-netplan-eth0.network",
                               **record(GENERATED, 0o640, content=False)}],
            "units": units, "legacy": legacy}


def snapshot(service=ESTONIA):
    return {"format": 1, "service_id": service, "ip": IP, "diagnostic": diagnostic(service),
            "netplan": record(RAW), "generated": record(GENERATED, 0o640), "cloud_init": None,
            "netplan_guard": {"macaddress": "52:54:00:00:00:01",
                              "generated_network_sha256": helper.digest(GENERATED),
                              "networkd_inputs": {}, "cloud_init_file_sha256": helper.digest(CLOUD)}}


def fake_netplan():
    def settings(value):
        result = dict(value)
        result["interfaces_file"] = Path(value["interfaces_file"])
        result["state_directory"] = Path(value["state_directory"])
        return SimpleNamespace(**result)

    return SimpleNamespace(CLOUD_CONFIG=Path("/etc/cloud/cloud.cfg.d/99-spn-ip-failover-network.cfg"),
                           CLOUD_DISABLED=CLOUD, Settings=SimpleNamespace(from_value=Mock(side_effect=settings)),
                           canonical_netplan=Mock(return_value=AFTER), generate_candidate=Mock(return_value=GENERATED))


def booted_snapshot():
    current = snapshot()
    current["netplan"] = record(AFTER)
    current["generated"] = record(GENERATED_AFTER, 0o644)
    current["cloud_init"] = record(CLOUD)
    current["netplan_guard"]["generated_network_sha256"] = helper.digest(GENERATED_AFTER)
    base = current["diagnostic"]
    base["units"] = base["units"].replace(" enabled enabled", " disabled enabled")
    base["legacy"]["active_state"] = "inactive"
    base["network"]["default"][0]["prefsrc"] = IP
    base["network_files"] = [{"path": str(enroll.YAML), **record(AFTER, content=False)},
                             {"path": "/run/systemd/network/10-netplan-eth0.network",
                              **record(GENERATED_AFTER, 0o644, content=False)}]
    return current


class NetplanPlanTests(unittest.TestCase):
    def validate(self, value, netplan=None):
        return enroll.validate_snapshot(value, value["service_id"], IP, OLD, helper, Mock(), netplan or fake_netplan())

    def test_plan_uses_netplan_explicitly_and_only_runs_isolated_candidate_generator(self):
        netplan = fake_netplan()
        with patch.object(helper, "run") as external, patch.object(helper, "write_once") as write:
            settings, rendered, value, unit = self.validate(snapshot(), netplan)
        self.assertEqual(settings.method, "netplan")
        self.assertEqual(value["bootstrap"]["active"], {"id": IP, "ip": IP})
        self.assertEqual(settings.interfaces_file, enroll.YAML)
        self.assertEqual(rendered, AFTER)
        self.assertEqual(unit, "spn-reality-source-" + enroll.SERVICES[ESTONIA] + ".service")
        self.assertEqual([call.args for call in netplan.generate_candidate.call_args_list],
                         [(RAW, settings), (AFTER, settings)])
        external.assert_not_called()
        write.assert_not_called()

    def test_manual_sweden_requires_no_source_unit(self):
        settings, _, _, unit = self.validate(snapshot(SWEDEN))
        self.assertEqual(settings.server_id, SWEDEN)
        self.assertIsNone(unit)
        value = snapshot(SWEDEN)
        value["diagnostic"]["units"] = "spn-reality-source-" + "a" * 32 + ".service enabled enabled"
        with self.assertRaisesRegex(ValueError, "Unexpected legacy"):
            self.validate(value)

    def test_wrong_snapshot_service_or_old_ip_are_never_inferred(self):
        value = snapshot()
        with self.assertRaisesRegex(ValueError, "another VPS"):
            enroll.validate_snapshot(value, SWEDEN, IP, OLD, helper, Mock(), fake_netplan())
        with self.assertRaisesRegex(ValueError, "explicitly pinned"):
            enroll.validate_snapshot(value, ESTONIA, IP, IP, helper, Mock(), fake_netplan())

    def test_source_code_and_unit_template_must_be_exact(self):
        for mutate in (lambda v: v["diagnostic"]["legacy"]["source"].update(sha256="a" * 64),
                       lambda v: v["diagnostic"]["legacy"].update(unit=record(b"ExecStart=arbitrary\n", 0o644)),
                       lambda v: v["diagnostic"]["legacy"].update(stage_plan=record(b"{}"))):
            value = snapshot()
            mutate(value)
            with self.assertRaises(ValueError):
                self.validate(value)

    def test_wrong_network_manager_default_extra_ip_policy_or_source_blocks_plan(self):
        for mutate in (lambda v: v["diagnostic"]["managers"].update(NetworkManager="active"),
                       lambda v: v["diagnostic"]["network"]["default"][0].update(gateway="10.0.0.2"),
                       lambda v: v["diagnostic"]["network"]["selected"][0].update(prefsrc=OLD),
                       lambda v: v["diagnostic"]["network"]["addresses"][0]["addr_info"].append(
                           {"family": "inet", "local": OLD, "prefixlen": 32}),
                       lambda v: v["diagnostic"]["network"]["rules"].append({"priority": 10, "table": "other"})):
            value = snapshot()
            mutate(value)
            with self.assertRaises(ValueError):
                self.validate(value)

    def test_foreign_cloud_marker_or_generated_guard_hash_blocks_plan(self):
        for mutate in (lambda v: v.update(cloud_init=record(b"network: {config: enabled}\n")),
                       lambda v: v["netplan_guard"].update(generated_network_sha256="a" * 64),
                       lambda v: v["netplan_guard"].update(cloud_init_file_sha256="b" * 64)):
            value = snapshot()
            mutate(value)
            with self.assertRaises(ValueError):
                self.validate(value)

    def test_pending_unrelated_yaml_edits_cannot_be_claimed_as_owned_generation(self):
        netplan = fake_netplan()
        netplan.generate_candidate.return_value = b"a different networkd definition\n"
        with self.assertRaisesRegex(ValueError, "pinned original YAML"):
            self.validate(snapshot(), netplan)
        netplan.canonical_netplan.assert_not_called()

    def test_retry_only_accepts_owned_disabled_unit_exact_marker_and_exact_render(self):
        pinned = snapshot()
        current = copy.deepcopy(pinned)
        current["netplan"] = record(AFTER)
        current["cloud_init"] = record(CLOUD)
        current["diagnostic"]["units"] = current["diagnostic"]["units"].replace(" enabled enabled", " disabled enabled")
        current["diagnostic"]["network_files"][0] = {"path": str(enroll.YAML), **record(AFTER, content=False)}
        current["diagnostic"]["network"]["selected"][0]["uid"] = 123
        enroll.current_matches(current, pinned, AFTER, GENERATED, fake_netplan(), helper)
        current["diagnostic"]["network"]["selected"][0]["prefsrc"] = OLD
        with self.assertRaisesRegex(ValueError, "drifted"):
            enroll.current_matches(current, pinned, AFTER, GENERATED, fake_netplan(), helper)

    def test_interrupted_bootstrap_can_resume_after_boot_with_only_exact_owned_generation(self):
        pinned, current = snapshot(), booted_snapshot()
        enroll.current_matches(current, pinned, AFTER, GENERATED_AFTER, fake_netplan(), helper)
        for mutate in (lambda v: v.update(generated=record(b"foreign generation\n")),
                       lambda v: v["diagnostic"]["legacy"].update(active_state="activating"),
                       lambda v: v["diagnostic"]["network"]["default"][0].update(gateway="10.0.0.2"),
                       lambda v: v.update(netplan=record(RAW)),
                       lambda v: v.update(cloud_init=None),
                       lambda v: v["diagnostic"].update(units=v["diagnostic"]["units"].replace(" disabled enabled", " enabled enabled"))):
            changed = copy.deepcopy(current)
            mutate(changed)
            with self.subTest(change=mutate), self.assertRaises(ValueError):
                enroll.current_matches(changed, pinned, AFTER, GENERATED_AFTER, fake_netplan(), helper)


class NetplanApplyTests(unittest.TestCase):
    def apply_mocked(self, *, current=None, guest_error=None, after_boot=False):
        pinned, netplan = snapshot(), fake_netplan()
        if after_boot:
            netplan.generate_candidate.side_effect = lambda raw, settings: GENERATED if raw == RAW else GENERATED_AFTER
        settings, rendered, value, unit = enroll.validate_snapshot(pinned, ESTONIA, IP, OLD, helper, Mock(), netplan)
        events = []
        guest, store = Mock(), Mock()
        guest.run.return_value = {"ok": True}
        if guest_error:
            guest.run.side_effect = guest_error
        store.locked.return_value = nullcontext()
        netplan.FileStore, netplan.ManagedGuest, netplan.SystemKernel = Mock(return_value=store), Mock(return_value=guest), Mock()
        managed = Mock()

        def write(path, raw, _managed):
            events.append(("write", path, raw))

        def run(*args, **kwargs):
            events.append(("command", args))
            return "disabled"

        meta = SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1)
        with patch.object(helper, "create_directory"), patch.object(helper, "write_once", side_effect=write), \
                patch.object(helper, "run", side_effect=run), \
                patch.object(helper, "record", side_effect=lambda path, **kwargs: record(SOURCE) if path.name == "source.py" else None), \
                patch.object(enroll, "capture", return_value=current or pinned), patch.object(enroll.os, "open", return_value=99), \
                patch.object(enroll.os, "fstat", return_value=meta), patch.object(enroll.os, "close"), \
                patch.object(enroll.fcntl, "flock"):
            try:
                result = enroll.apply(pinned, settings, rendered, value, unit, OLD, helper, managed, netplan)
            except Exception as exc:
                result = exc
        return result, events, guest

    def test_backups_precede_only_own_unit_disable_never_restart_apply_or_reload(self):
        result, events, guest = self.apply_mocked()
        self.assertTrue(result["ready"])
        commands = [event[1] for event in events if event[0] == "command"]
        unit = "spn-reality-source-" + enroll.SERVICES[ESTONIA] + ".service"
        self.assertEqual(commands, [("/usr/bin/systemctl", "disable", unit), ("/usr/bin/systemctl", "is-enabled", unit)])
        first_command = next(i for i, event in enumerate(events) if event[0] == "command")
        names = {event[1].name for event in events[:first_command] if event[0] == "write"}
        self.assertTrue({"snapshot.json", "intent.json", "netplan-original.yaml", "networkd-original.network",
                         "cloud-init-before.json", "legacy-unit", "legacy-plan", "legacy-stage_plan", "legacy-source.py"} <= names)
        self.assertEqual([call.args[0]["operation"] for call in guest.run.call_args_list], ["bootstrap", "check"])
        self.assertEqual(events[-1][1].name, "complete.json")

    def test_snapshot_drift_never_disables_unit_or_writes_config(self):
        current = snapshot()
        current["diagnostic"]["network"]["selected"][0]["prefsrc"] = OLD
        result, events, guest = self.apply_mocked(current=current)
        self.assertIsInstance(result, ValueError)
        self.assertEqual([event for event in events if event[0] == "command"], [])
        self.assertEqual({event[1].name for event in events if event[0] == "write"}, {"snapshot.json", "intent.json"})
        guest.run.assert_not_called()

    def test_bootstrap_failure_leaves_durable_intent_and_backups_without_claiming_ready(self):
        result, events, _ = self.apply_mocked(guest_error=ValueError("interrupted"))
        self.assertIsInstance(result, ValueError)
        names = {event[1].name for event in events if event[0] == "write"}
        self.assertIn("intent.json", names)
        self.assertIn("netplan-original.yaml", names)
        self.assertNotIn("complete.json", names)

    def test_incomplete_enrollment_retries_after_boot_without_toggling_disabled_unit(self):
        result, events, guest = self.apply_mocked(current=booted_snapshot(), after_boot=True)
        self.assertTrue(result["ready"])
        self.assertEqual([event[1][1] for event in events if event[0] == "command"], ["is-enabled"])
        self.assertEqual([call.args[0]["operation"] for call in guest.run.call_args_list], ["bootstrap", "check"])

    def completed_apply(self, *, mutate_receipt=None, wrong_config=False, readiness=False):
        pinned, netplan = snapshot(), fake_netplan()
        settings, rendered, value, unit = enroll.validate_snapshot(pinned, ESTONIA, IP, OLD, helper, managed_guest, netplan)
        intent = {"format": 1, "service_id": ESTONIA, "ip": IP, "legacy_ip": OLD,
                  "snapshot_sha256": helper.digest(helper.encoded(pinned)),
                  "config_sha256": helper.digest(helper.encoded(value)), "ready": True}
        if mutate_receipt:
            mutate_receipt(intent)
        store, guest = Mock(), Mock()
        store.locked.return_value = nullcontext()
        store.state.return_value = {"active": {"id": OLD, "ip": OLD}}
        guest.run.return_value = {"ok": not readiness}
        netplan.FileStore, netplan.ManagedGuest, netplan.SystemKernel = Mock(return_value=store), Mock(return_value=guest), Mock()

        def read(path, **_kwargs):
            if path.name == "complete.json":
                return record(helper.encoded(intent))
            if path == enroll.CONFIG:
                return record(b"{}\n" if wrong_config else helper.encoded(value))
            raise AssertionError("Completed check must not read source backups or fresh capture")

        meta = SimpleNamespace(st_mode=0o100600, st_uid=0, st_nlink=1)
        with patch.object(helper, "create_directory"), patch.object(helper, "write_once") as write, \
                patch.object(helper, "run") as command, patch.object(helper, "record", side_effect=read), \
                patch.object(enroll, "capture") as capture, patch.object(enroll.os, "open", return_value=99), \
                patch.object(enroll.os, "fstat", return_value=meta), patch.object(enroll.os, "close"), \
                patch.object(enroll.fcntl, "flock"):
            try:
                result = enroll.apply(pinned, settings, rendered, value, unit, OLD, helper, managed_guest, netplan)
            except Exception as exc:
                result = exc
            write.assert_not_called()
            command.assert_not_called()
            capture.assert_not_called()
        return result, guest

    def test_completed_enrollment_only_runs_fresh_readiness_and_reports_current_owned_ip(self):
        result, guest = self.completed_apply()
        self.assertTrue(result["ready"])
        self.assertTrue(result["already_enrolled"])
        self.assertEqual(result["ip"], OLD)  # A later authorized runtime rotation may have changed it.
        self.assertEqual([call.args[0]["operation"] for call in guest.run.call_args_list], ["check"])

    def test_completed_receipt_config_and_readiness_cannot_be_bypassed(self):
        for kwargs in ({"mutate_receipt": lambda value: value.update(snapshot_sha256="a" * 64)},
                       {"wrong_config": True}, {"readiness": True}):
            result, _guest = self.completed_apply(**kwargs)
            self.assertIsInstance(result, ValueError)


if __name__ == "__main__":
    unittest.main()
