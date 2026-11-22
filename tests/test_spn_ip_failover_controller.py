from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from spn_ip_failover.config import Host, Server, load_config
from spn_ip_failover.controller import Conflict, Controller
from spn_ip_failover.dns import DNSRecord
from spn_ip_failover.probes import ProbeRound
from spn_ip_failover.provider import Allocation, ProviderError, ProviderNotSubmitted
from spn_ip_failover.state import Journal


OLD = "8.8.8.8"
REJECTED = "8.8.4.4"
NEW = "1.1.1.1"
HOST = Host("host", "ru-entry", 443, "inbound", "vpn.example.test", True)
DNS = DNSRecord("zone", "record", HOST.name)
SERVER = Server("pilot", "provider", "vps", OLD, "old-id", True, "node", None, (HOST,), (DNS,), "fingerprint")


class Panel:
    def __init__(self):
        self.host = {"uuid": HOST.uuid, "address": HOST.name, "port": HOST.port,
                     "inbound": {"configProfileInboundUuid": HOST.inbound}, "isDisabled": False}
        self.node = {"uuid": SERVER.node, "address": OLD, "isConnected": True, "isDisabled": False}
        self.extra_hosts = []
        self.visibility_calls = []

    def read_host(self, uuid):
        return next(record.copy() for record in self.list_hosts() if record["uuid"] == uuid)

    def read_node(self, uuid):
        return self.node.copy()

    def list_hosts(self):
        return [self.host, *self.extra_hosts]

    def list_nodes(self):
        return [self.node]

    def replace_node_address(self, uuid, expected, replacement):
        assert self.node["address"] in (expected, replacement)
        self.node["address"] = replacement

    def set_host_disabled(self, uuid, expected, replacement, *, expected_address, expected_port, expected_inbound):
        record = next(record for record in self.list_hosts() if record["uuid"] == uuid)
        assert record["address"] == expected_address
        assert record["port"] == expected_port
        assert record["inbound"]["configProfileInboundUuid"] == expected_inbound
        if record["isDisabled"] == replacement:
            return False
        assert record["isDisabled"] == expected
        record["isDisabled"] = replacement
        self.visibility_calls.append(replacement)
        return True


class DNSClient:
    current = OLD

    def read_ipv4(self, record):
        return self.current

    def replace_ipv4(self, record, expected, replacement):
        assert self.current in (expected, replacement)
        self.current = replacement


class Provider:
    def __init__(self, candidates):
        self.candidates = iter(candidates)
        self.requests = []
        self.prepared = []
        self.activations = []
        self.releases = []
        self.retirements = []
        self.assigned = {Allocation("old-id", OLD)}
        self.peak_assigned = 1

    def check(self, server_id):
        assert server_id == SERVER.server_id

    def allocate(self, server_id, request_id):
        self.requests.append(request_id)
        candidate = next(self.candidates)
        if candidate is None:
            raise ProviderError("outcome unconfirmed")
        self.assigned.add(candidate)
        self.peak_assigned = max(self.peak_assigned, len(self.assigned))
        return candidate

    def inventory(self, server_id):
        return tuple(self.assigned)

    def prepare(self, server_id, candidate):
        self.prepared.append(candidate)

    def activate(self, server_id, before, after):
        self.activations.append((before.ip, after.ip))

    def release(self, server_id, candidate):
        self.releases.append(candidate)
        self.assigned.discard(candidate)

    def retire(self, server_id, old, current):
        self.retirements.append((old, current))
        self.release(server_id, old)


class ControllerTest(unittest.TestCase):
    def test_confirmed_global_vpn_failure_is_hidden_without_buying_ips_and_restored_on_recovery(self):
        self.public_health = "down"
        self.cycle(2)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.panel.visibility_calls, [True])
        self.public_health = "healthy"
        self.cycle(2)
        self.assertEqual(self.panel.visibility_calls, [True, False])

    def test_regional_then_global_failure_does_not_count_as_two_global_failures(self):
        self.cycle()
        self.public_health = "down"
        self.cycle()
        self.assertEqual(self.panel.visibility_calls, [])
        self.assertEqual(self.provider.requests, [])
        self.cycle()
        self.assertEqual(self.panel.visibility_calls, [True])

    def test_owned_pending_dns_transaction_is_resumed_before_strict_single_address_read(self):
        class PendingDNS(DNSClient):
            pending = True
            resumed = 0

            def read_ipv4(inner, record):
                if inner.pending:
                    raise Conflict("two journalled addresses")
                return inner.current

            def resume_ipv4(inner, record, allowed):
                self.assertEqual(allowed, {OLD, NEW})
                inner.resumed += 1
                inner.pending = False
                inner.current = NEW

        dns = PendingDNS()
        self.controller.dns = dns
        old, new = Allocation("old-id", OLD), Allocation("new-id", NEW)
        self.provider.assigned.add(new)
        self.controller.publication(SERVER, OLD, NEW, expected_allocations=(old, new))
        self.assertEqual(dns.resumed, 1)
        self.assertEqual(self.panel.node["address"], NEW)
        self.assertEqual(dns.current, NEW)

    def test_panel_drift_blocks_pending_dns_recovery_before_any_dns_write(self):
        from unittest.mock import Mock
        dns = Mock()
        self.controller.dns = dns
        self.panel.node["address"] = "9.9.9.9"
        old, new = Allocation("old-id", OLD), Allocation("new-id", NEW)
        self.provider.assigned.add(new)
        with self.assertRaises(Conflict):
            self.controller.publication(SERVER, OLD, NEW, expected_allocations=(old, new))
        dns.resume_ipv4.assert_not_called()
        dns.replace_ipv4.assert_not_called()

    def test_managed_publication_requires_exact_declared_allocations_before_any_dns_resume(self):
        from unittest.mock import Mock
        dns = Mock()
        self.controller.dns = dns
        old, new = Allocation("old-id", OLD), Allocation("new-id", NEW)
        self.provider.assigned.add(new)
        invalid = (None, (old,), (old, new, Allocation("other", REJECTED)),
                   (old, old), (old.as_payload(), new.as_payload()),
                   (old, Allocation("wrong-ip", REJECTED)))
        for declared in invalid:
            with self.subTest(declared=declared), self.assertRaises(Conflict):
                self.controller.publication(SERVER, OLD, NEW, expected_allocations=declared)
        dns.resume_ipv4.assert_not_called()
        dns.replace_ipv4.assert_not_called()
        self.assertEqual(self.panel.node["address"], OLD)

    def test_missing_or_reassigned_candidate_blocks_dns_recovery_before_any_write(self):
        from unittest.mock import Mock
        dns = Mock()
        self.controller.dns = dns
        old, new = Allocation("old-id", OLD), Allocation("new-id", NEW)
        for assigned in ({old}, {old, Allocation("reassigned-id", NEW)}):
            with self.subTest(assigned=assigned), self.assertRaisesRegex(Conflict, "точных назначения"):
                self.provider.assigned = assigned
                self.controller.publication(SERVER, OLD, NEW, expected_allocations=(old, new))
        dns.resume_ipv4.assert_not_called()
        dns.replace_ipv4.assert_not_called()
        self.assertEqual(self.panel.node["address"], OLD)

    def test_every_preflight_reads_fresh_provider_inventory_before_dns_resume(self):
        from unittest.mock import Mock
        old, new = Allocation("old-id", OLD), Allocation("new-id", NEW)
        self.provider.inventory = Mock(side_effect=[(old, new), (old,)])
        dns = Mock(wraps=self.dns)
        dns.resume_ipv4 = Mock(return_value=False)
        self.controller.dns = dns
        self.controller.publication_preflight(SERVER, OLD, NEW, expected_allocations=(old, new))
        with self.assertRaises(Conflict):
            self.controller.publication_preflight(SERVER, OLD, NEW, expected_allocations=(old, new))
        self.assertEqual(self.provider.inventory.call_count, 2)
        dns.resume_ipv4.assert_called_once()
        dns.replace_ipv4.assert_not_called()

    def test_promote_does_not_resume_dns_for_a_candidate_removed_by_hosting(self):
        from unittest.mock import Mock
        dns = Mock()
        self.controller.dns = dns
        row = self.controller.state(SERVER)
        row["pending"] = {"phase": "promote", "candidate": {"id": "new-id", "ip": NEW},
                          "deadline": self.clock + 100, "affected": [HOST.uuid], "rejected": []}
        with self.assertRaises(Conflict):
            self.controller.advance(SERVER, row, self.provider, None)
        dns.resume_ipv4.assert_not_called()
        dns.replace_ipv4.assert_not_called()
        self.assertEqual(self.provider.activations, [])
        self.assertEqual(self.panel.node["address"], OLD)

    def test_undo_does_not_resume_dns_when_the_previous_address_is_no_longer_owned(self):
        from unittest.mock import Mock
        dns = Mock()
        self.controller.dns = dns
        self.provider.assigned = {Allocation("new-id", NEW)}
        self.panel.node["address"] = NEW
        row = self.controller.state(SERVER)
        row["pending"] = {"phase": "undo", "candidate": {"id": "new-id", "ip": NEW},
                          "affected": [HOST.uuid], "rejected": []}
        with self.assertRaises(Conflict):
            self.controller.advance(SERVER, row, self.provider, None)
        dns.resume_ipv4.assert_not_called()
        dns.replace_ipv4.assert_not_called()
        self.assertEqual(self.provider.activations, [])
        self.assertEqual(self.panel.node["address"], NEW)

    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.journal_path = Path(temporary) / "journal"
        self.journal = self.enterContext(Journal(self.journal_path))
        self.panel = Panel()
        self.dns = DNSClient()
        self.provider = Provider([Allocation("bad-id", REJECTED), Allocation("new-id", NEW)])
        self.clock = 100
        self.public_health = "ru_down"
        self.events = []
        self.config = SimpleNamespace(
            servers=(SERVER,), max_attempts=10, max_ips_per_server=10, fail_rounds=2, recover_rounds=2,
            verify_seconds=100, retire_seconds=300,
        )
        self.controller = Controller(self.config, self.panel, self.dns, {"provider": self.provider},
                                     self.journal, self.events.append, now=lambda: self.clock)

    def check_route(self, host, address, expected_ip=None):
        if expected_ip is not None:
            self.assertEqual(self.dns.current, expected_ip)
        if address == REJECTED or address.startswith("8.8.4."):
            return "ru_down"
        if address == NEW:
            return "healthy"
        self.assertEqual(address, HOST.name)
        return self.public_health

    def cycle(self, count=1):
        for _ in range(count):
            self.controller.cycle(SERVER, SimpleNamespace(test=self.check_route))

    def test_retains_rejected_until_working_candidate_is_verified_then_retires_old(self):
        self.cycle(2)
        self.assertEqual(self.provider.releases, [])
        self.assertEqual(self.panel.visibility_calls, [])
        self.assertEqual(self.provider.assigned, {Allocation("old-id", OLD), Allocation("bad-id", REJECTED)})
        self.assertEqual(self.panel.node["address"], OLD)
        self.cycle()
        self.assertEqual(self.panel.node["address"], NEW)
        self.assertEqual(self.dns.current, NEW)
        self.assertEqual(self.provider.activations, [(OLD, NEW)])
        self.public_health = "healthy"
        self.cycle(2)
        row = self.controller.state(SERVER)
        self.assertEqual(row["current"]["ip"], NEW)
        self.assertIsNone(row["pending"])
        self.assertEqual(self.provider.releases, [Allocation("bad-id", REJECTED)])
        self.cycle(2)
        self.assertEqual(self.panel.visibility_calls, [])
        self.assertEqual(len(self.provider.releases), 1)
        self.clock += 301
        self.cycle()
        self.assertEqual(self.provider.releases[-1], Allocation("old-id", OLD))
        self.assertEqual(self.provider.retirements, [(Allocation("old-id", OLD), Allocation("new-id", NEW))])

    def test_ambiguous_allocation_is_journalled_and_never_retried_automatically(self):
        self.provider.candidates = iter([None])
        self.cycle()
        with self.assertRaises(ProviderError):
            self.cycle()
        snapshot = Journal(self.journal_path).snapshot()["servers"][SERVER.key]
        self.assertEqual(snapshot["tries"], 1)
        self.assertEqual(snapshot["pending"]["phase"], "allocating")
        with self.assertRaises(Conflict):
            self.cycle()
        self.assertEqual(len(self.provider.requests), 1)
        self.controller.reconcile(SERVER, snapshot["pending"]["request_id"], None)
        self.assertEqual(self.controller.state(SERVER)["tries"], 1)

    def test_ten_purchases_fit_ten_slots_by_releasing_only_old_failed_candidate(self):
        candidates = [Allocation(f"rejected-{n}", f"8.8.4.{n}") for n in range(1, 11)]
        self.provider.candidates = iter(candidates)
        self.cycle(10)
        self.assertEqual(len(self.provider.requests), 9)
        self.assertEqual(len(self.provider.assigned), 10)
        self.assertFalse(self.provider.releases)
        self.assertFalse(self.panel.visibility_calls)
        self.cycle(5)
        self.assertEqual(len(self.provider.requests), 10)
        self.assertEqual(self.provider.releases, candidates)
        self.assertEqual(self.provider.peak_assigned, 10)
        self.assertEqual(self.panel.visibility_calls, [True])
        self.assertEqual(self.provider.assigned, {Allocation("old-id", OLD)})
        self.assertTrue(Journal(self.journal_path).snapshot()["servers"][SERVER.key]["exhausted"])
        self.cycle(5)
        self.assertEqual(len(self.provider.requests), 10)

    def test_cap_includes_addresses_that_controller_does_not_own(self):
        self.config.max_ips_per_server = 3
        existing = Allocation("already-on-vps", "9.9.9.9")
        self.provider.assigned.add(existing)
        self.cycle(4)
        self.assertEqual(len(self.provider.requests), 2)
        self.assertEqual(self.provider.releases, [Allocation("bad-id", REJECTED)])
        self.assertIn(existing, self.provider.assigned)
        self.assertNotIn(existing, self.provider.releases)

    def test_recovered_current_address_cancels_orders_and_releases_unpublished_candidate(self):
        self.cycle(2)
        self.public_health = "healthy"
        self.cycle()
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(self.provider.releases, [Allocation("bad-id", REJECTED)])
        self.assertFalse(self.panel.visibility_calls)

    def test_only_failed_host_is_disabled_when_another_host_on_vps_still_works(self):
        peer = Host("second", "peer", 2083, "peer-inbound", None, True)
        peer_record = {"uuid": peer.uuid, "address": OLD, "port": peer.port,
                       "inbound": {"configProfileInboundUuid": peer.inbound}, "isDisabled": False}
        self.panel.extra_hosts.append(peer_record)
        server = replace(SERVER, hosts=(HOST, peer))
        self.config.servers = (server,)
        self.config.max_ips_per_server = 2
        self.config.max_attempts = 1
        probe = SimpleNamespace(test=lambda host, *args: "healthy" if host == peer else self.check_route(host, *args))
        for _ in range(3):
            self.controller.cycle(server, probe)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(self.panel.host["isDisabled"])
        self.assertFalse(peer_record["isDisabled"])

    def test_slots_containing_only_current_and_unmanaged_addresses_stop_without_buying(self):
        self.config.max_ips_per_server = 2
        extra = Allocation("unmanaged", "9.9.9.9")
        self.provider.assigned.add(extra)
        self.cycle(5)
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(self.provider.releases, [])
        self.assertEqual(self.provider.assigned, {Allocation("old-id", OLD), extra})

    def test_healthy_cycles_do_not_replenish_attempts_or_exhaustion_permission(self):
        self.config.max_attempts = 1
        self.cycle(3)
        row = self.controller.state(SERVER)
        self.assertEqual(row["tries"], 1)
        self.assertTrue(row["exhausted"])
        self.public_health = "healthy"
        self.cycle(5)
        self.assertEqual(row["tries"], 1)
        self.assertTrue(row["exhausted"])
        self.public_health = "ru_down"
        self.cycle(5)
        self.assertEqual(len(self.provider.requests), 1)

    def test_reloaded_journal_does_not_reset_purchase_limit(self):
        self.config.max_attempts = 1
        self.cycle(3)
        snapshot = Journal(self.journal_path).snapshot()
        reloaded = SimpleNamespace(data=snapshot, save=lambda: None)
        controller = Controller(self.config, self.panel, self.dns, {"provider": self.provider},
                                reloaded, self.events.append, now=lambda: self.clock)
        self.public_health = "healthy"
        for _ in range(5):
            controller.cycle(SERVER, SimpleNamespace(test=self.check_route))
        self.public_health = "ru_down"
        for _ in range(5):
            controller.cycle(SERVER, SimpleNamespace(test=self.check_route))
        self.assertEqual(snapshot["servers"][SERVER.key]["tries"], 1)
        self.assertTrue(snapshot["servers"][SERVER.key]["exhausted"])
        self.assertEqual(len(self.provider.requests), 1)

    def test_explicit_operator_reset_only_when_no_pending_operation(self):
        self.cycle(2)
        with self.assertRaises(Conflict):
            self.controller.reset_attempts(SERVER)
        self.public_health = "healthy"
        self.cycle()
        row = self.controller.state(SERVER)
        self.assertIsNone(row["pending"])
        self.assertEqual(row["tries"], 1)
        self.controller.reset_attempts(SERVER)
        self.assertEqual(row["tries"], 0)
        self.assertFalse(row["exhausted"])
        self.assertEqual(row["limit_resets"], [{"at": self.clock, "previous_tries": 1}])

    def test_reissued_failed_address_counts_as_order_without_repeating_vpn_test(self):
        self.config.max_ips_per_server = 2
        self.config.max_attempts = 3
        repeated = Allocation("reissued-id", REJECTED)
        self.provider.candidates = iter([Allocation("bad-id", REJECTED), repeated, Allocation("new-id", NEW)])
        counts = {REJECTED: 0}

        def probe(host, address, expected_ip=None):
            if address == REJECTED:
                counts[REJECTED] += 1
            return self.check_route(host, address, expected_ip)

        for _ in range(7):
            self.controller.cycle(SERVER, SimpleNamespace(test=probe))
        self.assertEqual(len(self.provider.requests), 3)
        self.assertEqual(counts[REJECTED], 1)
        self.assertNotIn(repeated, self.provider.prepared)
        self.assertEqual(self.provider.releases[:2], [Allocation("bad-id", REJECTED), repeated])
        self.assertEqual(self.controller.state(SERVER)["rejected_history"], [REJECTED])

    def test_reconciled_reissued_failed_address_is_discarded_without_retest(self):
        from unittest.mock import Mock
        self.config.max_ips_per_server = 2
        self.cycle(3)
        self.provider.candidates = iter([None])
        with self.assertRaises(ProviderError):
            self.cycle()
        row = self.controller.state(SERVER)
        request = row["pending"]["request_id"]
        candidate = Allocation("reissued-id", REJECTED)
        self.provider.assigned.add(candidate)
        self.controller.reconcile(SERVER, request, candidate)
        self.assertEqual(row["pending"]["phase"], "discard")
        probe = SimpleNamespace(test=Mock(side_effect=AssertionError("No repeated VPN probe")))
        self.controller.advance(SERVER, row, self.provider, probe)
        self.assertEqual(row["pending"]["phase"], "seek")
        self.assertEqual(row["tries"], 2)
        probe.test.assert_not_called()

    def test_slot_release_lost_reply_retries_idempotent_finalization_without_second_purchase(self):
        self.config.max_ips_per_server = 2
        self.cycle(2)
        original = self.provider.release
        lost = [False]

        def release(server_id, candidate):
            original(server_id, candidate)
            if not lost[0]:
                lost[0] = True
                raise ProviderError("Lost reply after releasing failed candidate")

        self.provider.release = release
        with self.assertRaises(ProviderError):
            self.cycle()
        row = self.controller.state(SERVER)
        self.assertEqual(row["pending"]["phase"], "release_failed")
        self.assertEqual(len(self.provider.requests), 1)
        self.cycle()
        self.assertEqual(row["pending"]["phase"], "seek")
        self.assertEqual(self.provider.releases, [Allocation("bad-id", REJECTED)] * 2)
        self.cycle()
        self.assertEqual(len(self.provider.requests), 2)

    def test_slot_release_billing_success_but_guest_failure_still_finishes_guest_cleanup(self):
        self.config.max_ips_per_server = 2
        self.cycle(2)
        billing_deletes, guest_calls = [], []
        target = Allocation("bad-id", REJECTED)

        def release(server, allocation):
            if allocation in self.provider.assigned:
                self.provider.assigned.remove(allocation)
                billing_deletes.append(allocation)
            guest_calls.append(allocation)
            if len(guest_calls) == 1:
                raise ProviderError("Billing deleted the IP; guest deprovision failed")

        self.provider.release = release
        with self.assertRaises(ProviderError):
            self.cycle()
        self.assertNotIn(target, self.provider.assigned)
        self.assertEqual(self.controller.state(SERVER)["pending"]["phase"], "release_failed")
        self.cycle()
        self.assertEqual(billing_deletes, [target])
        self.assertEqual(guest_calls, [target, target])
        self.assertEqual(self.controller.state(SERVER)["pending"]["phase"], "seek")

    def test_slot_release_stops_on_external_inventory_or_host_reference_change(self):
        self.config.max_ips_per_server = 2
        self.cycle(2)
        self.panel.extra_hosts.append({"uuid": "outside-scope", "address": REJECTED})
        with self.assertRaises(Conflict):
            self.cycle()
        self.assertEqual(self.provider.releases, [])

        self.panel.extra_hosts.clear()
        self.provider.assigned.add(Allocation("external-add", "9.9.9.9"))
        with self.assertRaises(Conflict):
            self.cycle()
        self.assertEqual(self.provider.releases, [])

    def test_definitive_budget_denial_does_not_charge_purchase_attempt_or_spin(self):
        from unittest.mock import Mock
        self.cycle(2)
        self.provider.allocate = Mock(side_effect=ProviderNotSubmitted("Budget denied before order"))
        self.cycle()
        row = self.controller.state(SERVER)
        self.assertEqual(row["tries"], 1)
        self.assertEqual(row["pending"]["phase"], "budget_wait")
        self.cycle(5)
        self.assertEqual(self.provider.allocate.call_count, 1)
        self.assertEqual(self.provider.releases, [Allocation("bad-id", REJECTED)])
        self.assertEqual(self.provider.assigned, {Allocation("old-id", OLD)})

    def test_budget_retry_waits_an_hour_and_requires_fresh_russian_failure(self):
        from unittest.mock import Mock
        self.provider.allocate = Mock(side_effect=ProviderNotSubmitted("Budget denied"))
        self.cycle(2)
        self.assertEqual(self.controller.state(SERVER)["tries"], 0)
        self.clock += 3601
        self.public_health = "unknown"
        self.cycle(5)
        self.assertEqual(self.provider.allocate.call_count, 1)
        self.public_health = "ru_down"
        self.cycle()
        self.assertEqual(self.provider.allocate.call_count, 2)
        self.assertEqual(self.controller.state(SERVER)["tries"], 0)

    def test_budget_wait_recovery_stops_further_orders_without_resetting_attempts(self):
        from unittest.mock import Mock
        self.cycle(2)
        self.provider.allocate = Mock(side_effect=ProviderNotSubmitted("Budget denied"))
        self.cycle()
        self.public_health = "healthy"
        self.cycle()
        row = self.controller.state(SERVER)
        self.assertIsNone(row["pending"])
        self.assertEqual(row["tries"], 1)
        self.assertEqual(self.provider.releases, [Allocation("bad-id", REJECTED)])

    def test_budget_cleanup_lost_release_reply_stays_recoverable(self):
        from unittest.mock import Mock
        self.cycle(2)
        self.provider.allocate = Mock(side_effect=ProviderNotSubmitted("Budget denied"))
        self.cycle()
        original = self.provider.release
        lost = [False]

        def release(server, allocation):
            original(server, allocation)
            if not lost[0]:
                lost[0] = True
                raise ProviderError("Lost reply after deletion")

        self.provider.release = release
        with self.assertRaises(ProviderError):
            self.cycle()
        row = self.controller.state(SERVER)
        self.assertEqual(row["pending"]["phase"], "release_failed")
        self.cycle(2)
        self.assertEqual(row["pending"]["phase"], "budget_wait")
        self.assertEqual(self.provider.releases, [Allocation("bad-id", REJECTED)] * 2)
        self.assertEqual(self.provider.allocate.call_count, 1)

    def test_stale_probe_at_cap_cleans_up_before_waiting_to_hide(self):
        self.config.max_ips_per_server = 2
        self.cycle(2)
        self.public_health = "unknown"
        self.cycle()
        self.assertEqual(self.provider.releases, [Allocation("bad-id", REJECTED)])
        self.assertEqual(len(self.provider.requests), 1)
        self.assertFalse(self.panel.visibility_calls)

    def test_failed_publication_check_rolls_back_before_candidate_cleanup(self):
        self.provider.candidates = iter([Allocation("new-id", NEW)])
        self.config.max_attempts = 1
        self.cycle(2)
        self.clock += 101
        self.cycle()
        self.assertEqual(self.provider.activations, [(OLD, NEW), (NEW, OLD)])
        self.assertEqual(self.dns.current, OLD)
        self.assertFalse(self.provider.releases)
        self.clock += 301
        self.cycle()
        self.assertEqual(self.provider.releases, [Allocation("new-id", NEW)])

    def test_candidate_used_by_unmanaged_host_is_not_prepared_or_released(self):
        self.panel.extra_hosts = [{"uuid": "other", "address": REJECTED}]
        self.cycle()
        with self.assertRaises(Conflict):
            self.cycle()
        self.assertEqual(self.provider.prepared, [])
        self.assertEqual(self.provider.releases, [])

    def test_foreign_inventory_reference_blocks_paid_changes(self):
        self.panel.extra_hosts = [{"uuid": "other", "address": HOST.name}]
        self.cycle()
        with self.assertRaises(Conflict):
            self.cycle()
        self.assertEqual(self.provider.requests, [])

    def test_uncertain_checks_and_previously_disabled_host_do_not_change_visibility(self):
        self.public_health = "unknown"
        self.panel.host["isDisabled"] = True
        self.cycle(4)
        self.public_health = "healthy"
        self.cycle(4)
        self.assertEqual(self.panel.visibility_calls, [])
        self.assertEqual(self.provider.requests, [])


class ProbeRoundTest(unittest.TestCase):
    def test_down_control_makes_regional_failure_unconfirmed(self):
        def probe(name, region, target, control="pass"):
            return SimpleNamespace(name=name, region=region,
                                   run=lambda payload: control if payload["action"] == "control" else target)

        good = probe("ru-one", "ru", "down"), probe("ru-two", "ru", "down"), probe("external", "outside", "pass")
        self.assertEqual(ProbeRound(good).test(HOST, OLD), "ru_down")
        bad = (good[0], probe("ru-two", "ru", "down", "unknown"), good[2])
        self.assertEqual(ProbeRound(bad).test(HOST, OLD), "unknown")

    def test_sample_is_accepted_for_planning_only(self):
        example = Path(__file__).resolve().parents[1] / "spn_ip_failover" / "config.example.json"
        self.assertEqual(load_config(example, allow_example=True).max_attempts, 10)
        with self.assertRaises(ValueError):
            load_config(example)

    def test_one_russian_probe_and_external_control_are_supported(self):
        example = Path(__file__).resolve().parents[1] / "spn_ip_failover" / "config.example.json"
        data = json.loads(example.read_text())
        data["probes"] = [data["probes"][0], data["probes"][-1]]
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "inventory.json"
            inventory.write_text(json.dumps(data))
            self.assertEqual(len(load_config(inventory, allow_example=True).probes), 2)


if __name__ == "__main__":
    unittest.main()
