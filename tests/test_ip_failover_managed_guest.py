import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from spn_ip_failover.managed_guest import (
    FileStore,
    ManagedGuest,
    ManagedGuestError,
    Settings,
    SystemKernel,
    canonical_interfaces,
    digest,
    read_root_file,
    unique_object,
)


OLD = {"id": "initial-allocation", "ip": "2.26.148.130"}
NEW = {"id": "new-allocation", "ip": "8.8.4.4"}
OTHER = {"id": "other-allocation", "ip": "9.9.9.9"}
SERVER = "poland-service"
V6 = "iface ens3 inet6 static\n    address 2a00:1450:4001::1/64\n    gateway 2a00:1450:4001::ffff\n"
UNMANAGED = "auto eth7\niface eth7 inet static\n    address 10.7.0.2/24\n    post-up /usr/bin/true\n"
RAW = ("# existing configuration\nauto lo\niface lo inet loopback\n\n"
       "auto ens3\niface ens3 inet static\n    address 191.217.245.62/32\n"
       "    gateway 10.0.0.1\n    hwaddress ether 52:54:00:ab:cd:ef\n"
       "    dns-nameservers 1.1.1.1 8.8.8.8\n"
       "auto ens3:0\niface ens3:0 inet static\n    address 191.217.245.225\n"
       "    netmask 255.255.255.255\n" + V6 + UNMANAGED).encode()


def settings(raw=RAW, owned=None):
    owned = owned or [OLD]
    return Settings.from_value({
        "version": 1, "server_id": SERVER, "interface": "ens3", "gateway": "10.0.0.1",
        "method": "ifupdown", "interfaces_file": "/etc/network/interfaces",
        "state_directory": "/var/lib/spn-managed-guest",
        "bootstrap": {"expected_config_sha256": digest(raw), "expected_live_ipv4": [row["ip"] for row in owned],
                      "active": OLD, "allocations": owned},
    })


class Crash(BaseException):
    pass


class MemoryStore:
    def __init__(self, raw=RAW):
        self.raw = raw
        self.saved = None
        self.original = None
        self.events = []
        self.after_config = None
        self.after_save = None

    def config(self):
        return self.raw

    def state(self):
        return copy.deepcopy(self.saved)

    def save(self, value):
        self.events.append(("journal", value["pending"] is not None))
        self.saved = copy.deepcopy(value)
        if self.after_save:
            self.after_save(value)

    def write_config(self, expected, raw):
        if digest(self.raw) != expected:
            raise ManagedGuestError("Interfaces changed concurrently")
        self.events.append(("config", digest(raw)))
        self.raw = raw
        if self.after_config:
            self.after_config()

    def backup(self, raw):
        self.events.append(("backup", digest(raw)))
        if self.original is not None and self.original != raw:
            raise ManagedGuestError("backup drift")
        self.original = raw


class FakeKernel:
    def __init__(self, ips=None):
        self.ips = set(ips or [OLD["ip"]])
        self.current_route = {"dst": "default", "gateway": "10.0.0.1", "dev": "ens3", "flags": ["onlink"]}
        self.implicit_source = OLD["ip"]
        self.events = []
        self.fail_after = None
        self.before_snapshot = None

    def snapshot(self):
        if self.before_snapshot:
            self.before_snapshot()
        return {"ips": sorted(self.ips), "route": copy.deepcopy(self.current_route),
                "source": self.current_route.get("prefsrc", self.implicit_source)}

    def effect(self, operation):
        if self.fail_after == operation:
            self.fail_after = None
            raise Crash(operation)

    def add(self, ip):
        if ip in self.ips:
            raise AssertionError("Duplicate live address add")
        self.events.append(("add", ip))
        self.ips.add(ip)
        self.effect("add")

    def remove(self, ip):
        if ip not in self.ips:
            raise AssertionError("Duplicate live address removal")
        self.events.append(("remove", ip))
        self.ips.remove(ip)
        self.effect("remove")

    def route(self, before, source):
        if self.current_route != before:
            raise AssertionError("Stale route mutation")
        self.events.append(("route", source))
        self.current_route = {**before, "prefsrc": source}
        self.effect("route")


def prepare(row=NEW):
    return {"operation": "prepare", "server_id": SERVER, **row}


def activate(old=OLD, new=NEW):
    return {"operation": "activate", "server_id": SERVER, "old": old, "new": new}


def deprovision(row=OLD):
    return {"operation": "deprovision", "server_id": SERVER, **row, "billing_confirmed": True}


class RenderTests(unittest.TestCase):
    def test_normalizes_stale_base_and_alias_without_touching_ipv6_loopback_or_other_interface(self):
        result = canonical_interfaces(RAW, settings(), [OLD], OLD).decode()
        self.assertIn("auto lo\niface lo inet loopback\n", result)
        self.assertIn(V6, result)
        self.assertIn(UNMANAGED, result)
        self.assertIn("iface ens3 inet static\n    address 2.26.148.130/32\n", result)
        self.assertNotIn("191.217.245.", result)
        self.assertNotIn("    gateway 10.0.0.1", result)
        self.assertIn("    hwaddress ether 52:54:00:ab:cd:ef\n", result)
        self.assertIn("    dns-nameservers 1.1.1.1 8.8.8.8\n", result)
        self.assertEqual(result.count("route replace default"), 1)

    def test_canonical_aliases_and_repeated_render_are_stable(self):
        config = settings()
        first = canonical_interfaces(RAW, config, [OLD, NEW, OTHER], NEW)
        second = canonical_interfaces(first, config, [OLD, NEW, OTHER], NEW)
        self.assertEqual(first, second)
        text = first.decode()
        self.assertIn("iface ens3 inet static\n    address 8.8.4.4/32\n", text)
        self.assertIn("iface ens3:1 inet static\n    address 2.26.148.130/32\n", text)
        self.assertIn("iface ens3:2 inet static\n    address 9.9.9.9/32\n", text)
        self.assertEqual(text.count("post-up /usr/sbin/ip"), 1)

    def test_ipv6_alias_activation_is_preserved_and_ipv4_aliases_do_not_reuse_it(self):
        alias = "auto ens3:1\niface ens3:1 inet6 static\n    address 2a00:1450:4001::2/64\n"
        raw = RAW + alias.encode()
        result = canonical_interfaces(raw, settings(raw), [OLD, NEW], OLD)
        self.assertIn(alias.encode(), result)
        self.assertIn(b"auto ens3:2\niface ens3:2 inet static\n    address 8.8.4.4/32\n", result)
        self.assertEqual(canonical_interfaces(result, settings(raw), [OLD, NEW], OLD), result)

    def test_refuses_unrecognized_commands_includes_aliases_and_ambiguous_options(self):
        additions = [
            "    pre-up /usr/bin/true\n", "    up echo test\n", "    post-up /usr/bin/true\n",
            "    address 8.8.8.8/32\n", "    netmask 255.255.255.0\n", "    mtu 1500\n",
        ]
        bad = [RAW.replace(b"    gateway 10.0.0.1\n", text.encode()) for text in additions]
        bad += [RAW + b"source /etc/network/interfaces.d/*\n", RAW + b"mapping ens3\n",
                RAW.replace(b"ens3:0", b"ens3:foo"), RAW.replace(b"inet static", b"inet dhcp", 1),
                RAW.replace(b"    gateway 10.0.0.1", b"    gateway 10.0.0.2"),
                RAW.replace(V6.encode(), (V6 + "    source /root/hidden\n").encode())]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ManagedGuestError):
                canonical_interfaces(value, settings(), [OLD], OLD)


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.config = settings()
        self.store = MemoryStore()
        self.kernel = FakeKernel()
        self.guest = ManagedGuest(self.config, self.store, self.kernel)
        self.bootstrap = {"operation": "bootstrap", "server_id": SERVER, "apply": True}

    def initialized(self):
        self.guest.run(self.bootstrap)
        self.store.events.clear()
        self.kernel.events.clear()

    def test_check_requires_bootstrap_and_has_no_side_effects(self):
        request = {"operation": "check", "server_id": SERVER}
        with self.assertRaisesRegex(ManagedGuestError, "Bootstrap"):
            self.guest.run(request)
        self.initialized()
        previous = copy.deepcopy(self.store.saved)
        self.assertEqual(self.guest.run(request), {"ok": True})
        self.assertEqual(self.store.saved, previous)
        self.assertEqual(self.store.events + self.kernel.events, [])

    def test_check_refuses_drift_or_pending_without_resuming_it(self):
        self.initialized()
        self.store.raw += b"# changed\n"
        with self.assertRaisesRegex(ManagedGuestError, "Interfaces"):
            self.guest.run({"operation": "check", "server_id": SERVER})
        self.store.raw = canonical_interfaces(RAW, self.config, [OLD], OLD)
        self.store.saved["pending"] = {"request": prepare()}
        with self.assertRaisesRegex(ManagedGuestError, "pending"):
            self.guest.run({"operation": "check", "server_id": SERVER})
        self.assertEqual(self.store.events + self.kernel.events, [])

    def test_bootstrap_plan_has_no_persistent_or_live_side_effect(self):
        result = self.guest.run({**self.bootstrap, "apply": False})
        self.assertTrue(result["planned"])
        self.assertEqual(result["before_config_sha256"], digest(RAW))
        self.assertNotEqual(result["after_config_sha256"], digest(RAW))
        self.assertIsNone(self.store.saved)
        self.assertIsNone(self.store.original)
        self.assertEqual(self.store.events + self.kernel.events, [])
        self.assertEqual(self.store.raw, RAW)

    def test_bootstrap_pins_hash_and_exact_live_set(self):
        self.store.raw += b"# concurrent edit\n"
        with self.assertRaises(ManagedGuestError):
            self.guest.run(self.bootstrap)
        self.store.raw = RAW
        self.kernel.ips.add(OTHER["ip"])
        with self.assertRaises(ManagedGuestError):
            self.guest.run(self.bootstrap)
        self.assertEqual(self.store.events + self.kernel.events, [])

    def test_bootstrap_saves_original_and_pending_before_config_without_touching_route(self):
        before_route = copy.deepcopy(self.kernel.current_route)
        result = self.guest.run(self.bootstrap)
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.original, RAW)
        self.assertEqual([event[0] for event in self.store.events], ["backup", "journal", "config", "journal"])
        self.assertTrue(self.store.events[1][1])
        self.assertIsNone(self.store.saved["pending"])
        self.assertEqual(self.kernel.events, [])
        self.assertEqual(self.kernel.current_route, before_route)
        self.assertTrue(self.guest.run(self.bootstrap)["already_bootstrapped"])

    def test_prepare_adds_and_persists_candidate_without_default_source_change(self):
        self.initialized()
        before = copy.deepcopy(self.kernel.current_route)
        result = self.guest.run(prepare())
        self.assertEqual(result["owned_ips"], sorted([OLD["ip"], NEW["ip"]]))
        self.assertEqual(self.kernel.current_route, before)
        self.assertEqual(self.kernel.snapshot()["source"], OLD["ip"])
        self.assertEqual(self.kernel.events, [("add", NEW["ip"])])
        self.assertIn((NEW["ip"] + "/32").encode(), self.store.raw)
        self.assertTrue(self.guest.run(prepare())["already_prepared"])
        self.assertEqual(len(self.kernel.events), 1)

    def test_activate_preserves_both_addresses_and_is_idempotent(self):
        self.initialized()
        self.guest.run(prepare())
        result = self.guest.run(activate())
        self.assertEqual(result["active_ip"], NEW["ip"])
        self.assertEqual(self.kernel.ips, {OLD["ip"], NEW["ip"]})
        self.assertEqual(self.kernel.events, [("add", NEW["ip"]), ("route", NEW["ip"])])
        self.assertIn(("iface ens3 inet static\n    address " + NEW["ip"] + "/32\n").encode(), self.store.raw)
        self.assertTrue(self.guest.run(activate())["already_active"])
        self.assertEqual(len(self.kernel.events), 2)

    def test_deprovision_requires_billing_confirmation_and_refuses_active_address(self):
        self.initialized()
        self.guest.run(prepare())
        before = copy.deepcopy(self.store.saved)
        for request in (deprovision(), {**deprovision(NEW), "billing_confirmed": False},
                        {**deprovision(NEW), "billing_confirmed": 1}):
            with self.subTest(request=request), self.assertRaises(ManagedGuestError):
                self.guest.run(request)
        self.assertEqual(self.store.saved, before)
        self.assertEqual(self.kernel.events, [("add", NEW["ip"])])

    def test_deprovision_cleans_nonactive_and_handles_already_removed_by_hoster(self):
        self.initialized()
        self.guest.run(prepare())
        self.guest.run(activate())
        self.kernel.ips.remove(OLD["ip"])
        result = self.guest.run(deprovision())
        self.assertEqual(result["owned_ips"], [NEW["ip"]])
        self.assertNotIn(OLD["ip"].encode(), self.store.raw)
        self.assertFalse(any(event[0] == "remove" for event in self.kernel.events))
        self.assertTrue(self.guest.run(deprovision())["already_deprovisioned"])

    def test_crash_after_persisted_candidate_can_resume_only_identical_request(self):
        self.initialized()
        self.store.after_config = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.guest.run(prepare())
        self.assertIsNotNone(self.store.saved["pending"])
        self.assertNotIn(NEW["ip"], self.kernel.ips)
        with self.assertRaisesRegex(ManagedGuestError, "exact request"):
            self.guest.run(prepare(OTHER))
        self.store.after_config = None
        self.guest.run(prepare())
        self.assertIsNone(self.store.saved["pending"])
        self.assertEqual(self.kernel.events, [("add", NEW["ip"])])

    def test_crash_after_live_add_does_not_duplicate_address_on_replay(self):
        self.initialized()
        self.kernel.fail_after = "add"
        with self.assertRaises(Crash):
            self.guest.run(prepare())
        self.guest.run(prepare())
        self.assertEqual(self.kernel.events, [("add", NEW["ip"])])
        self.assertIsNone(self.store.saved["pending"])

    def test_crash_after_route_change_does_not_overwrite_or_repeat_route_on_replay(self):
        self.initialized()
        self.guest.run(prepare())
        self.kernel.fail_after = "route"
        with self.assertRaises(Crash):
            self.guest.run(activate())
        self.assertEqual(self.kernel.current_route["prefsrc"], NEW["ip"])
        self.guest.run(activate())
        self.assertEqual([event for event in self.kernel.events if event[0] == "route"], [("route", NEW["ip"])])
        self.assertEqual(self.store.saved["active"], NEW)

    def test_crash_after_live_removal_does_not_repeat_delete(self):
        self.initialized()
        self.guest.run(prepare())
        self.guest.run(activate())
        self.kernel.fail_after = "remove"
        with self.assertRaises(Crash):
            self.guest.run(deprovision())
        self.guest.run(deprovision())
        self.assertEqual([event for event in self.kernel.events if event[0] == "remove"], [("remove", OLD["ip"])])

    def test_pending_recovery_after_reboot_accepts_our_post_up_source_and_prepared_alias(self):
        self.initialized()
        self.store.after_config = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.guest.run(prepare())
        self.store.after_config = None
        # ifupdown applied the persisted aliases and the exact managed post-up during boot.
        self.kernel.ips.add(NEW["ip"])
        self.kernel.current_route["prefsrc"] = OLD["ip"]
        self.guest.run(prepare())
        self.assertEqual(self.kernel.events, [])
        self.assertEqual(self.store.saved["route"]["prefsrc"], OLD["ip"])

    def test_pending_recovery_refuses_concurrent_config_route_or_address_edits(self):
        def config_edit():
            self.store.raw += b"# external operator edit\n"

        def route_edit():
            self.kernel.current_route["metric"] = 100

        def address_edit():
            self.kernel.ips.add(OTHER["ip"])

        for edit in (config_edit, route_edit, address_edit):
            with self.subTest(edit=edit.__name__):
                self.setUp()
                self.initialized()
                self.store.after_config = lambda: (_ for _ in ()).throw(Crash())
                with self.assertRaises(Crash):
                    self.guest.run(prepare())
                self.store.after_config = None
                edit()
                current = self.store.raw
                with self.assertRaises(ManagedGuestError):
                    self.guest.run(prepare())
                self.assertEqual(self.store.raw, current)
                self.assertEqual(self.kernel.events, [])

    def test_route_edit_during_config_commit_is_detected_before_address_add(self):
        self.initialized()
        self.store.after_config = lambda: self.kernel.current_route.update(metric=100)
        with self.assertRaisesRegex(ManagedGuestError, "route changed"):
            self.guest.run(prepare())
        self.assertEqual(self.kernel.events, [])
        self.assertIsNotNone(self.store.saved["pending"])

    def test_unknown_ids_addresses_and_eleventh_ip_cannot_change_interface(self):
        self.initialized()
        for request in (prepare({"id": OLD["id"], "ip": NEW["ip"]}),
                        prepare({"id": NEW["id"], "ip": OLD["ip"]}),
                        prepare({"id": "bad;command", "ip": NEW["ip"]}),
                        {**prepare(), "server_id": "different-vps"},
                        {**prepare(), "command": "echo danger"}, activate()):
            with self.subTest(request=request), self.assertRaises(ManagedGuestError):
                self.guest.run(request)
        owned = [OLD] + [{"id": f"extra-{n}", "ip": f"8.8.8.{n}"} for n in range(1, 10)]
        self.config = settings(owned=owned)
        self.store = MemoryStore()
        self.kernel = FakeKernel([row["ip"] for row in owned])
        self.guest = ManagedGuest(self.config, self.store, self.kernel)
        self.guest.run(self.bootstrap)
        with self.assertRaisesRegex(ManagedGuestError, "Ten-address"):
            self.guest.run(prepare())

    def test_corrupt_pending_destination_cannot_add_unrequested_addresses(self):
        self.initialized()
        self.store.after_config = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.guest.run(prepare())
        self.store.after_config = None
        self.store.saved["pending"]["destination"]["allocations"].append(OTHER)
        with self.assertRaises(ManagedGuestError):
            self.guest.run(prepare())
        self.assertEqual(self.kernel.events, [])


class KernelAndInputTests(unittest.TestCase):
    def setUp(self):
        self.config = settings()
        self.kernel = SystemKernel(self.config)
        self.rules = [{"priority": priority, "src": "all", "table": table}
                      for priority, table in [(0, "local"), (32766, "main"), (32767, "default")]]
        self.links = [{"ifname": "ens3", "flags": ["UP"],
                       "addr_info": [{"family": "inet", "local": OLD["ip"], "prefixlen": 32}]}]
        self.route = {"dst": "default", "gateway": "10.0.0.1", "dev": "ens3", "flags": ["onlink"]}

    def snapshot(self):
        self.kernel.manager = Mock()
        self.kernel.ip = Mock(side_effect=[self.rules, self.links, [self.route],
                                          [{"dev": "ens3", "gateway": "10.0.0.1", "prefsrc": OLD["ip"]}]])
        return self.kernel.snapshot()

    def test_accepts_actual_pilot_implicit_source_route(self):
        value = self.snapshot()
        self.assertEqual(value["route"], self.route)
        self.assertNotIn("prefsrc", value["route"])
        self.assertEqual(value["source"], OLD["ip"])

    def test_refuses_nonstandard_rules_prefix_and_route(self):
        for mutation in (lambda: self.rules.append({"priority": 100, "src": "all", "table": "other"}),
                         lambda: self.links[0]["addr_info"][0].update(prefixlen=24),
                         lambda: self.route.update(metric=100),
                         lambda: self.route.update(protocol="dhcp"),
                         lambda: self.route.update(gateway="10.0.0.2"),
                         lambda: self.route.update(dev="eth0")):
            with self.subTest(mutation=mutation):
                self.setUp()
                mutation()
                with self.assertRaises(ManagedGuestError):
                    self.snapshot()

    def test_active_networkd_must_report_pinned_interface_unmanaged(self):
        self.kernel.run = Mock(side_effect=["active", "ens3=ens3", "inactive", "active", "2 ens3 ether routable unmanaged", ""])
        self.kernel.manager()
        self.kernel.run = Mock(side_effect=["active", "ens3=ens3", "inactive", "active", "2 ens3 ether routable configured"])
        with self.assertRaisesRegex(ManagedGuestError, "networkd"):
            self.kernel.manager()
        self.kernel.run = Mock(side_effect=["active", "ens3=ens3", "active"])
        with self.assertRaisesRegex(ManagedGuestError, "NetworkManager"):
            self.kernel.manager()

    def test_enabled_or_unknown_legacy_source_writer_prevents_readiness(self):
        unit = "spn-reality-source-" + "a" * 32 + ".service"
        for output, accepted in (("", True), (unit + " disabled enabled", True),
                                 (unit + " masked enabled", True), (unit + " enabled enabled", False),
                                 (unit + " static -", False), ("unexpected.service disabled -", False)):
            with self.subTest(output=output):
                self.kernel.run = Mock(side_effect=["active", "ens3=ens3", "inactive", "inactive", output])
                if accepted:
                    self.kernel.manager()
                else:
                    with self.assertRaisesRegex(ManagedGuestError, "preferred-source"):
                        self.kernel.manager()

    def test_network_commands_are_fixed_argv_without_shell_interpolation(self):
        self.kernel.run = Mock()
        self.kernel.snapshot = Mock(return_value={"route": self.route, "ips": [OLD["ip"], NEW["ip"]]})
        self.kernel.add(NEW["ip"])
        self.kernel.route(self.route, NEW["ip"])
        self.kernel.remove(OLD["ip"])
        self.assertEqual(self.kernel.run.call_args_list[0].args,
                         ("/usr/sbin/ip", "-4", "address", "add", "8.8.4.4/32", "dev", "ens3"))
        with self.assertRaises(ManagedGuestError):
            self.kernel.add("8.8.8.8;id")

    def test_route_change_rechecks_snapshot_before_replacing(self):
        self.kernel.run = Mock()
        self.kernel.snapshot = Mock(return_value={"route": {**self.route, "metric": 99}, "ips": [NEW["ip"]]})
        with self.assertRaisesRegex(ManagedGuestError, "before replacement"):
            self.kernel.route(self.route, NEW["ip"])
        self.kernel.run.assert_not_called()

    def test_duplicate_json_and_unsupported_root_configuration_are_rejected(self):
        with self.assertRaises(ManagedGuestError):
            json.loads('{"operation":"prepare","operation":"activate"}', object_pairs_hook=unique_object)
        value = {"version": 1, "server_id": SERVER, "interface": "ens3", "gateway": "10.0.0.1",
                 "method": "netplan", "interfaces_file": "/etc/network/interfaces",
                 "state_directory": "/var/lib/spn-managed-guest", "bootstrap": self.config.bootstrap}
        with self.assertRaises(ManagedGuestError):
            Settings.from_value(value)

    def test_symlink_config_is_never_followed_even_with_trusted_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("not a configuration")
            link = Path(directory) / "link"
            link.symlink_to(target)
            with patch("spn_ip_failover.managed_guest.secure_parents"), self.assertRaises(OSError):
                read_root_file(link, 100, private=True)

    def test_file_store_refuses_journal_or_config_drift_before_atomic_write(self):
        store = FileStore(self.config)
        store.state_raw = b"expected journal"
        info = Mock(st_mode=0o100644, st_gid=0)
        with patch("spn_ip_failover.managed_guest.read_root_file", return_value=(b"external journal", info)), \
                patch("spn_ip_failover.managed_guest.atomic_write") as write:
            with self.assertRaisesRegex(ManagedGuestError, "journal changed"):
                store.save({"different": True})
            write.assert_not_called()

    def test_file_store_rechecks_after_preparing_temporary_config(self):
        store = FileStore(self.config)
        info = Mock(st_mode=0o100644, st_gid=0)

        def prepare_temporary(*_args, before_replace, **_kwargs):
            # Simulate an operator edit while the durable temporary was written.
            before_replace()

        with patch("spn_ip_failover.managed_guest.read_root_file",
                   side_effect=[(RAW, info), (RAW + b"# operator\n", info)]), \
                patch("spn_ip_failover.managed_guest.atomic_write", side_effect=prepare_temporary):
            with self.assertRaisesRegex(ManagedGuestError, "before commit"):
                store.write_config(digest(RAW), b"replacement")
        with patch("spn_ip_failover.managed_guest.read_root_file", return_value=(b"external configuration", info)), \
                patch("spn_ip_failover.managed_guest.atomic_write") as write:
            with self.assertRaisesRegex(ManagedGuestError, "Interfaces changed"):
                store.write_config(digest(RAW), b"replacement")
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
