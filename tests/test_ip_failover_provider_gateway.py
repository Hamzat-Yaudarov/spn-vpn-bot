import unittest
from unittest.mock import Mock

from spn_ip_failover.provider import ProviderError
from spn_ip_failover.provider_gateway import Gateway


NODE = "44e7dd20-4e42-48ff-ab6d-48cd3bad9b39"
OLD = {"id": "8.8.8.8", "ip": "8.8.8.8"}
NEW = {"id": "8.8.4.4", "ip": "8.8.4.4"}


def configuration(retire=True):
    return {"version": 1, "remnawave": {}, "servers": {"vps": {
        "node_uuid": NODE, "billing_command": ["/usr/bin/python3", "-m", "billing"],
        "guest_ssh": ["/usr/bin/ssh", "-l", "root", "{address}"], "supports_retire": retire}}}


class GatewayTests(unittest.TestCase):
    def gateway(self, replies, *, active=OLD, retire=True):
        panel = Mock()
        panel.read_node.return_value = {"address": active["ip"], "isDisabled": False}
        run = Mock(side_effect=replies)
        return Gateway(configuration(retire), panel, run=run), run

    def test_check_requires_read_only_guest_readiness_before_billing_check(self):
        gateway, run = self.gateway([{"allocations": [OLD]}, {"ok": True}, {"ok": True}])
        request = {"operation": "check", "server_id": "vps"}
        self.assertEqual(gateway.execute(request), {"ok": True})
        self.assertEqual(run.call_args_list[1].args, (["/usr/bin/ssh", "-l", "root", OLD["ip"]], request))
        self.assertEqual(run.call_args_list[2].args[1], request)

    def test_each_allocation_checks_guest_before_sending_any_paid_operation(self):
        request = {"operation": "allocate", "server_id": "vps", "request_id": "once"}
        gateway, run = self.gateway([{"allocations": [OLD]}, {"ok": True}, NEW])
        self.assertEqual(gateway.execute(request), NEW)
        self.assertEqual(run.call_args_list[1].args[1], {"operation": "check", "server_id": "vps"})
        self.assertEqual(run.call_args_list[2].args[1], request)
        gateway, run = self.gateway([{"allocations": [OLD]}, {"ok": False}])
        with self.assertRaisesRegex(ProviderError, "network helper"):
            gateway.execute(request)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[1]["operation"] != "allocate" for call in run.call_args_list))

    def test_prepare_verifies_assignment_and_only_runs_the_pinned_guest(self):
        gateway, run = self.gateway([{"allocations": [OLD, NEW]}, {"ok": True}])
        request = {"operation": "prepare", "server_id": "vps", **NEW}
        self.assertEqual(gateway.execute(request), {"ok": True})
        self.assertEqual(run.call_args_list[0].args[1], {"operation": "inventory", "server_id": "vps"})
        self.assertEqual(run.call_args_list[1].args, (["/usr/bin/ssh", "-l", "root", OLD["ip"]], request))
        gateway, run = self.gateway([{"allocations": [OLD]}])
        with self.assertRaises(ProviderError):
            gateway.execute(request)
        self.assertEqual(run.call_count, 1)

    def test_active_published_address_is_never_sent_for_billing_removal(self):
        gateway, run = self.gateway([{"allocations": [OLD, NEW]}])
        with self.assertRaisesRegex(ProviderError, "active published"):
            gateway.execute({"operation": "release", "server_id": "vps", **OLD})
        self.assertEqual(run.call_count, 1)

    def test_billing_failure_never_deprovisions_guest(self):
        gateway, run = self.gateway([{"allocations": [OLD, NEW]}, {"ok": False}])
        with self.assertRaises(ProviderError):
            gateway.execute({"operation": "release", "server_id": "vps", **NEW})
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[0][0] != "/usr/bin/ssh" for call in run.call_args_list))

    def test_removal_is_read_back_before_deprovision_with_billing_confirmation(self):
        gateway, run = self.gateway([{"allocations": [OLD, NEW]}, {"ok": True},
                                    {"allocations": [OLD]}, {"ok": True}])
        self.assertEqual(gateway.execute({"operation": "release", "server_id": "vps", **NEW}), {"ok": True})
        self.assertEqual(run.call_args.args[1], {"operation": "deprovision", "server_id": "vps",
                                              **NEW, "billing_confirmed": True})
        gateway, run = self.gateway([{"allocations": [OLD, NEW]}, {"ok": True}, {"allocations": [OLD, NEW]}])
        with self.assertRaisesRegex(ProviderError, "still lists"):
            gateway.execute({"operation": "release", "server_id": "vps", **NEW})
        self.assertEqual(run.call_count, 3)

    def test_retire_uses_new_published_peer_and_confirms_replacement_survived(self):
        gateway, run = self.gateway([{"allocations": [OLD, NEW]}, {"ok": True},
                                    {"allocations": [NEW]}, {"ok": True}], active=NEW)
        request = {"operation": "retire", "server_id": "vps", "old": OLD, "current": NEW}
        self.assertEqual(gateway.execute(request), {"ok": True})
        self.assertEqual(run.call_args_list[1].args[1], request)
        self.assertEqual(run.call_args.args[0][-1], NEW["ip"])
        self.assertEqual(run.call_args.args[1]["ip"], OLD["ip"])

    def test_non_primary_billing_retire_maps_to_release_for_provider_without_retire(self):
        gateway, run = self.gateway([{"allocations": [OLD, NEW]}, {"ok": True},
                                    {"allocations": [NEW]}, {"ok": True}], active=NEW, retire=False)
        gateway.execute({"operation": "retire", "server_id": "vps", "old": OLD, "current": NEW})
        self.assertEqual(run.call_args_list[1].args[1], {"operation": "release", "server_id": "vps", **OLD})

    def test_management_address_must_belong_to_provider_inventory(self):
        gateway, run = self.gateway([{"allocations": [NEW]}])
        with self.assertRaisesRegex(ProviderError, "not an assigned"):
            gateway.execute({"operation": "prepare", "server_id": "vps", **NEW})
        self.assertEqual(run.call_count, 1)

    def test_unknown_server_and_extra_mutation_fields_do_not_reach_mutations(self):
        gateway, run = self.gateway([])
        with self.assertRaises(ValueError):
            gateway.execute({"operation": "allocate", "server_id": "other", "request_id": "a"})
        with self.assertRaises(ValueError):
            gateway.execute({"operation": "allocate", "server_id": "vps", "request_id": "a", "postpaid": True})
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
