import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from spn_ip_failover.config import load_config
from spn_ip_failover.dns import DNSRecord, DNSUpdateError
from spn_ip_failover.dns_router import DNSRouter
from spn_ip_failover.regru_dns import RegRuRecord


class DNSRouterTests(unittest.TestCase):
    def test_record_identity_selects_backend_and_only_reg_resumes_its_own_pending_change(self):
        cf, reg = Mock(spec=["read_ipv4", "replace_ipv4"]), Mock(spec=["read_ipv4", "replace_ipv4", "resume_ipv4"])
        router = DNSRouter(cloudflare=cf, regru=reg)
        cloud = DNSRecord("zone", "record", "vpn.example.test")
        registered = RegRuRecord("example.test", "vpn", "vpn.example.test")
        router.read_ipv4(cloud)
        router.replace_ipv4(registered, "8.8.8.8", "1.1.1.1")
        self.assertFalse(router.resume_ipv4(cloud, {"8.8.8.8", "1.1.1.1"}))
        router.resume_ipv4(registered, {"8.8.8.8", "1.1.1.1"})
        cf.read_ipv4.assert_called_once_with(cloud)
        cf.replace_ipv4.assert_not_called()
        reg.replace_ipv4.assert_called_once_with(registered, "8.8.8.8", "1.1.1.1")
        reg.resume_ipv4.assert_called_once_with(registered, {"8.8.8.8", "1.1.1.1"})

    def test_missing_or_unknown_provider_never_falls_back_to_another_account(self):
        router = DNSRouter(cloudflare=Mock())
        for record in (RegRuRecord("example.test", "vpn", "vpn.example.test"), object()):
            with self.assertRaises(DNSUpdateError):
                router.read_ipv4(record)

    def test_config_can_pin_reg_record_and_explicit_origin_alias_of_the_same_vps(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "spn_ip_failover/config.example.json").read_text())
        config["servers"][0]["dns"] = [
            {"provider": "regru", "zone_id": "example.invalid", "record_id": "vpn", "name": "vpn.example.invalid"},
            {"provider": "regru", "zone_id": "example.invalid", "record_id": "origin", "name": "origin.example.invalid"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config))
            loaded = load_config(path, allow_example=True)
            self.assertTrue(all(isinstance(record, RegRuRecord) for record in loaded.servers[0].dns))
            config["servers"][0]["dns"][0]["provider"] = "unknown"
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_config(path, allow_example=True)


if __name__ == "__main__":
    unittest.main()
