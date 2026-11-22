import unittest
from unittest.mock import Mock

from spn_ip_failover.guest import run_request


OLD = {"id": "old", "ip": "8.8.8.8"}
NEW = {"id": "new", "ip": "1.1.1.1"}
SERVER = "12345"
INTERFACE = "ens3"


class GuestTests(unittest.TestCase):
    def setUp(self):
        self.move = Mock()
        self.move.ip_json.return_value = [{
            "ifname": INTERFACE,
            "flags": ["UP"],
            "addr_info": [{"family": "inet", "local": item["ip"]} for item in (OLD, NEW)],
        }]
        self.move.default_route.return_value = {"dev": INTERFACE}
        self.prepare = {"operation": "prepare", "server_id": SERVER, **NEW}
        self.activate = {"operation": "activate", "server_id": SERVER, "old": OLD, "new": NEW}

    def test_prepare_checks_ifupdown_without_writing(self):
        run_request(self.move, SERVER, INTERFACE, self.prepare)
        self.move.migrate.assert_called_once_with(NEW["ip"], apply=False, ifupdown=True)

    def test_prepare_refuses_unassigned_address_or_wrong_vps(self):
        self.move.ip_json.return_value[0]["flags"] = []
        for server_id in (SERVER, "another-vps"):
            with self.subTest(server_id=server_id), self.assertRaises(ValueError):
                run_request(self.move, server_id, INTERFACE, self.prepare)
        self.move.migrate.assert_not_called()

    def test_activate_refuses_unexpected_live_source(self):
        self.move.selected_source.return_value = "9.9.9.9"
        with self.assertRaises(ValueError):
            run_request(self.move, SERVER, INTERFACE, self.activate)
        self.move.migrate.assert_not_called()

    def test_activate_checks_readback_and_can_reapply_after_interruption(self):
        self.move.selected_source.side_effect = [OLD["ip"], NEW["ip"], NEW["ip"], NEW["ip"]]
        run_request(self.move, SERVER, INTERFACE, self.activate)
        run_request(self.move, SERVER, INTERFACE, self.activate)
        self.assertEqual(self.move.migrate.call_count, 2)
        self.move.migrate.assert_called_with(NEW["ip"], apply=True, ifupdown=True)

    def test_activate_requires_old_address_to_remain_available(self):
        self.move.ip_json.return_value[0]["addr_info"].pop(0)
        with self.assertRaises(ValueError):
            run_request(self.move, SERVER, INTERFACE, self.activate)
        self.move.migrate.assert_not_called()
