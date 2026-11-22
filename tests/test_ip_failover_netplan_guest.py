from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from spn_ip_failover import managed_guest as base
from spn_ip_failover import netplan_guest as netplan


OLD = {"id": "8.8.8.8", "ip": "8.8.8.8"}
NEW = {"id": "8.8.4.4", "ip": "8.8.4.4"}
OTHER = {"id": "9.9.9.9", "ip": "9.9.9.9"}
MAC = "52:54:00:ea:f7:4a"
RAW = b"""network:
  ethernets:
    eth0:
      addresses:
      - 8.8.8.8/32
      - 2001:4860:4860::8888/64
      dhcp4: false
      match:
        macaddress: 52:54:00:ea:f7:4a
      nameservers:
        addresses: [1.1.1.1, '2606:4700:4700::1111']
      optional: true
      routes:
      - on-link: true
        to: 0.0.0.0/0
        via: 10.0.0.1
      - on-link: true
        to: ::/0
        via: 2001:4860:4860::1
      set-name: eth0
      wakeonlan: false
  version: 2
"""
GEN = ("[Match]\nPermanentMACAddress=" + MAC + "\nName=eth0\n\n"
       "[Network]\nAddress=8.8.8.8/32\nAddress=2001:4860:4860::8888/64\n"
       "[Route]\nDestination=0.0.0.0/0\nGateway=10.0.0.1\nGatewayOnLink=true\n\n"
       "[Route]\nDestination=::/0\nGateway=2001:4860:4860::1\nGatewayOnLink=true\n").encode()


def configuration():
    return {"version": 1, "server_id": "service", "interface": "eth0", "gateway": "10.0.0.1",
            "method": "netplan", "interfaces_file": "/etc/netplan/50-cloud-init.yaml",
            "state_directory": "/var/lib/spn-ip-failover-guest", "bootstrap": {
                "expected_config_sha256": base.digest(RAW), "expected_live_ipv4": [OLD["ip"]],
                "active": OLD, "allocations": [OLD]}, "netplan_guard": {
                    "macaddress": MAC, "generated_network_sha256": base.digest(GEN),
                    "networkd_inputs": {}, "cloud_init_file_sha256": base.digest(netplan.CLOUD_DISABLED)}}


def settings():
    return netplan.Settings.from_value(configuration())


@unittest.skipUnless(importlib.util.find_spec("yaml"), "Native Netplan YAML dependency required for semantic tests")
class NetplanRenderTests(unittest.TestCase):
    def test_changes_only_owned_ipv4_addresses_and_preferred_source(self):
        before = netplan.parse_yaml(RAW)
        rendered = netplan.canonical_netplan(RAW, settings(), [OLD, NEW, OTHER], NEW)
        after = netplan.parse_yaml(rendered)
        eth = after["network"]["ethernets"]["eth0"]
        self.assertEqual(eth["addresses"], [NEW["ip"] + "/32", OLD["ip"] + "/32", OTHER["ip"] + "/32",
                                            "2001:4860:4860::8888/64"])
        self.assertEqual(eth["routes"][0]["from"], NEW["ip"])
        stripped = deepcopy(after)
        stripped_eth = stripped["network"]["ethernets"]["eth0"]
        stripped_eth["addresses"] = before["network"]["ethernets"]["eth0"]["addresses"]
        del stripped_eth["routes"][0]["from"]
        self.assertEqual(stripped, before)
        self.assertEqual(netplan.canonical_netplan(rendered, settings(), [OLD, NEW, OTHER], NEW), rendered)

    def test_netplan_settings_do_not_weaken_ifupdown_configuration(self):
        self.assertEqual(settings().interface, "eth0")
        with self.assertRaises(base.ManagedGuestError):
            base.Settings.from_value(configuration())
        for change in (lambda x: x.update(method="guess"),
                       lambda x: x.update(interfaces_file="/etc/network/interfaces"),
                       lambda x: x["netplan_guard"].update(macaddress="any"),
                       lambda x: x["netplan_guard"].update(cloud_init_file_sha256="a" * 64),
                       lambda x: x["netplan_guard"].update(networkd_inputs={"/tmp/evil.network": "a" * 64})):
            value = configuration()
            change(value)
            with self.assertRaises(base.ManagedGuestError):
                netplan.Settings.from_value(value)

    def test_duplicate_keys_anchors_and_unsafe_tags_are_rejected(self):
        for raw in (RAW.replace(b"dhcp4: false", b"dhcp4: false\n      dhcp4: true"),
                    RAW.replace(b"addresses:\n", b"addresses: &a\n", 1),
                    b"network: !!python/object/apply:os.system ['true']\n"):
            with self.assertRaises(base.ManagedGuestError):
                netplan.canonical_netplan(raw, settings(), [OLD], OLD)

    def test_unknown_ethernet_dhcp_routes_prefix_or_mac_cannot_be_guessed(self):
        for raw in (RAW.replace(b"dhcp4: false", b"dhcp4: true"),
                    RAW.replace(b"8.8.8.8/32", b"8.8.8.8/24"),
                    RAW.replace(b"set-name: eth0", b"set-name: eth1"),
                    RAW.replace(b"52:54:00:ea:f7:4a", b"52:54:00:ea:f7:4b"),
                    RAW.replace(b"via: 10.0.0.1", b"via: 10.0.0.2"),
                    RAW.replace(b"optional: true", b"unknown-command: true"),
                    RAW.replace(b"to: 0.0.0.0/0", b"to: 8.8.0.0/16")):
            with self.assertRaises(base.ManagedGuestError):
                netplan.canonical_netplan(raw, settings(), [OLD], OLD)

    def test_native_generated_preferred_source_is_verified_not_assumed(self):
        netplan.verify_generated(GEN, RAW, settings())
        rendered = netplan.canonical_netplan(RAW, settings(), [OLD], OLD)
        generated = GEN.replace(b"Gateway=10.0.0.1\n", b"Gateway=10.0.0.1\nPreferredSource=8.8.8.8\n")
        netplan.verify_generated(generated, rendered, settings())
        for raw in (GEN, generated.replace(b"PreferredSource=8.8.8.8", b"PreferredSource=8.8.4.4"),
                    generated.replace(b"GatewayOnLink=true", b"GatewayOnLink=false"),
                    generated + b"\n[RoutingPolicyRule]\nFrom=8.8.8.8/32\n",
                    generated.replace(b"Address=2001:4860:4860::8888/64\n", b"")):
            with self.assertRaises(base.ManagedGuestError):
                netplan.verify_generated(raw, rendered, settings())

    def test_candidate_generation_only_uses_private_alternate_root(self):
        command_seen = []
        temporary = tempfile.TemporaryDirectory
        def generate(argv, **kwargs):
            command_seen.append(argv)
            self.assertEqual(argv[:3], ["/usr/sbin/netplan", "generate", "--root-dir"])
            root = Path(argv[3])
            self.assertNotEqual(root, Path("/"))
            self.assertEqual((root / "etc/netplan/50-cloud-init.yaml").read_bytes(), RAW)
            target = root / "run/systemd/network/10-netplan-eth0.network"
            target.parent.mkdir(parents=True)
            target.write_bytes(GEN)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        with patch.object(netplan.subprocess, "run", side_effect=generate), \
                patch.object(base, "read_root_file", return_value=(GEN, None)), \
                patch.object(base, "secure_parents"), \
                patch.object(netplan.tempfile, "TemporaryDirectory", side_effect=lambda **kwargs: temporary()):
            self.assertEqual(netplan.generate_candidate(RAW, settings()), GEN)
        self.assertEqual(len(command_seen), 1)
        self.assertFalse(Path(command_seen[0][3]).exists())


class NetplanGenerationHistoryTests(unittest.TestCase):
    def test_history_keeps_running_render_across_multiple_changes_and_reboot(self):
        config = settings()
        store = netplan.FileStore(config)
        gen1, gen2, gen3, gen4 = (value.encode() for value in ("generated1", "generated2", "generated3", "generated4"))
        for running, committed, candidate, previous in (
            (gen1, gen2, gen3, [base.digest(gen1), base.digest(gen2)]),
            (gen1, gen3, gen4, [base.digest(gen1), base.digest(gen2), base.digest(gen3)]),
            (gen3, gen3, gen4, [base.digest(gen1), base.digest(gen2), base.digest(gen3)])):
            store.config = Mock(return_value=b"current")
            with patch.object(netplan, "generate_candidate", side_effect=[candidate, committed]), \
                    patch.object(base, "read_root_file", return_value=(running, None)), \
                    patch.object(netplan, "generation_history", return_value=(b"old-history", previous)), \
                    patch.object(base, "atomic_write") as write, \
                    patch.object(base.FileStore, "write_config") as replace:
                store.write_config(base.digest(b"current"), b"candidate")
                value = json.loads(write.call_args.args[1])
                self.assertEqual(set(value["sha256s"]), {base.digest(running), base.digest(committed), base.digest(candidate)})
                self.assertLessEqual(len(value["sha256s"]), 3)
                replace.assert_called_once_with(base.digest(b"current"), b"candidate")

    def test_unapproved_running_generation_never_replaces_yaml(self):
        store = netplan.FileStore(settings())
        store.config = Mock(return_value=b"current")
        with patch.object(netplan, "generate_candidate", return_value=b"approved"), \
                patch.object(base, "read_root_file", return_value=(b"unapproved", None)), \
                patch.object(netplan, "generation_history", return_value=(None, [])), \
                patch.object(base, "atomic_write") as write, \
                patch.object(base.FileStore, "write_config") as replace:
            with self.assertRaisesRegex(base.ManagedGuestError, "not owned"):
                store.write_config(base.digest(b"current"), b"candidate")
            write.assert_not_called()
            replace.assert_not_called()


class NetplanKernelTests(unittest.TestCase):
    def manager(self, *, unit="", changed_manifest=False, changed_cloud=False, generated=GEN, network_manager="inactive"):
        kernel = netplan.SystemKernel(settings())
        def run(*argv, **_kwargs):
            if argv[:2] == ("/usr/bin/systemctl", "is-active"):
                return {"networking": "inactive", "NetworkManager": network_manager, "systemd-networkd": "active"}[argv[2]]
            if "list-unit-files" in argv:
                return unit
            if "list" in argv:
                return "2 eth0 ether routable configured"
            return " Network File: /run/systemd/network/10-netplan-eth0.network"
        kernel.run = Mock(side_effect=run)
        with patch.object(netplan, "yaml_inventory", return_value=[settings().interfaces_file]), \
                patch.object(netplan, "networkd_manifest", return_value={"new": "changed"} if changed_manifest else {}), \
                patch.object(netplan, "cloud_guard", return_value="a" * 64 if changed_cloud else base.digest(netplan.CLOUD_DISABLED)), \
                patch.object(Path, "read_text", return_value=MAC + "\n"), \
                patch.object(base, "read_root_file", side_effect=[(RAW, None), (generated, None)]), \
                patch.object(netplan, "generate_candidate", return_value=GEN), \
                patch.object(netplan, "generation_history", return_value=(None, [])):
            kernel.manager()
        return kernel

    def test_native_configured_networkd_and_no_legacy_units_are_supported(self):
        kernel = self.manager()
        self.assertEqual(kernel.route_protocols, ("static", 4))

    def test_ownership_input_and_generation_drift_fail_closed(self):
        for kwargs in ({"unit": "spn-reality-source-" + "a" * 32 + ".service enabled enabled"},
                       {"changed_manifest": True}, {"changed_cloud": True}, {"generated": b"changed"},
                       {"network_manager": "active"}):
            with self.assertRaises(base.ManagedGuestError):
                self.manager(**kwargs)

    def test_no_match_systemd_exit_one_only_means_empty_with_no_diagnostic(self):
        kernel = netplan.SystemKernel(settings())
        args = ("/usr/bin/systemctl", "list-unit-files", "--no-pager", "--no-legend", "spn-reality-source-*.service")
        with patch.object(base.subprocess, "run", return_value=subprocess.CompletedProcess(args, 1, "", "")):
            self.assertEqual(kernel.run(*args, allowed=(0, 1)), "")
        for stdout, stderr in (("unexpected output", ""), ("", "permission error")):
            with patch.object(base.subprocess, "run", return_value=subprocess.CompletedProcess(args, 1, stdout, stderr)):
                with self.assertRaises(base.ManagedGuestError):
                    kernel.run(*args, allowed=(0, 1))


class NetplanCloudOwnershipTests(unittest.TestCase):
    def test_future_cloud_marker_must_precede_no_later_config(self):
        with patch.object(Path, "glob", return_value=[Path("/etc/cloud/cloud.cfg.d/90-default.cfg")]):
            netplan.cloud_order()
        with patch.object(Path, "glob", return_value=[Path("/etc/cloud/cloud.cfg.d/zz-override.cfg")]):
            with self.assertRaisesRegex(base.ManagedGuestError, "later cloud-init"):
                netplan.cloud_order()

    def test_kernel_network_override_blocks_claiming_persistence(self):
        with patch.object(Path, "read_text", return_value="BOOT_IMAGE=/vmlinuz root=UUID=123 ro console=ttyS0\n"):
            netplan.cloud_kernel_guard()
        with patch.object(Path, "read_text", return_value="root=/dev/sda network-config=disabled\n"):
            netplan.cloud_kernel_guard()
        for extra in ("ip=dhcp", "network-config=abc123"):
            with patch.object(Path, "read_text", return_value="root=/dev/sda " + extra):
                with self.assertRaisesRegex(base.ManagedGuestError, "Kernel boot arguments"):
                    netplan.cloud_kernel_guard()

    def test_enrollment_never_accepts_a_different_existing_marker(self):
        with patch.object(netplan, "cloud_kernel_guard"), patch.object(netplan, "cloud_order"), \
                patch.object(base, "read_root_file", return_value=(b"network: {}\n", None)):
            with self.assertRaisesRegex(base.ManagedGuestError, "explicitly handed off"):
                netplan.cloud_guard()


if __name__ == "__main__":
    unittest.main()
