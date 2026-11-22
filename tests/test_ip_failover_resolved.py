from copy import deepcopy
import unittest

from spn_ip_failover.resolved import xray_outbound


def row(network="ws", security="tls"):
    settings = {"serverName": "vpn.example.invalid", "fingerprint": "chrome"}
    if security == "tls":
        settings.update(alpn="h2,http/1.1", enableSessionResumption=False)
    else:
        settings.update(publicKey="fixture-public-key", shortId="ab12")
    transports = {
        "ws": {"host": "vpn.example.invalid", "path": "/ws", "headers": None},
        "grpc": {"authority": "vpn.example.invalid", "serviceName": "grpc", "multiMode": False},
        "tcp": {"header": None},
        "xhttp": {"host": "vpn.example.invalid", "path": "/assets/", "mode": "packet-up",
                  "extra": {"xmux": {"maxConnections": "1"}}},
    }
    return {"protocol": "vless", "protocolOptions": {"id": "fixture-user", "encryption": "none", "flow": ""},
            "address": "vpn.example.invalid", "port": 443, "transport": network,
            "transportOptions": transports[network], "security": security, "securityOptions": settings,
            "clientOverrides": {"xrayJsonTemplate": None, "mapper": None},
            "streamOverrides": {"finalMask": None, "sockopt": None}, "mux": None}


class ResolvedTests(unittest.TestCase):
    def test_ws_host_and_tls_identity_are_preserved_without_mutating_input(self):
        source = row()
        before = deepcopy(source)
        outbound = xray_outbound(source)
        self.assertEqual(source, before)
        self.assertEqual(outbound["settings"]["vnext"][0]["users"], [{"id": "fixture-user", "encryption": "none"}])
        stream = outbound["streamSettings"]
        self.assertEqual(stream["tlsSettings"]["alpn"], ["h2", "http/1.1"])
        self.assertEqual(stream["wsSettings"]["headers"], {"Host": "vpn.example.invalid"})
        self.assertFalse(stream["tlsSettings"]["enableSessionResumption"])

    def test_supported_transports_and_reality_keep_required_options(self):
        for transport in ("tcp", "ws", "grpc", "xhttp"):
            for security in ("tls", "reality"):
                with self.subTest(transport=transport, security=security):
                    outbound = xray_outbound(row(transport, security))
                    self.assertEqual(outbound["streamSettings"]["network"], transport)
                    self.assertEqual(outbound["streamSettings"]["security"], security)
        grpc = xray_outbound(row("grpc", "reality"))["streamSettings"]
        self.assertEqual(grpc["grpcSettings"]["serviceName"], "grpc")
        self.assertEqual(grpc["realitySettings"]["publicKey"], "fixture-public-key")

    def test_virtual_and_dependent_configurations_are_refused(self):
        mutations = [
            lambda r: r.update(protocol="shadowsocks"),
            lambda r: r["clientOverrides"].update(xrayJsonTemplate="custom"),
            lambda r: r["clientOverrides"].update(mapper={"set": "custom"}),
            lambda r: r["streamOverrides"].update(sockopt={"dialerProxy": "upstream"}),
            lambda r: r.update(mux={"enabled": True}),
            lambda r: r["transportOptions"]["extra"].update(downloadSettings={"address": "other.invalid"}),
            lambda r: r["transportOptions"]["extra"].update(sockopt={"dialerProxy": "other"}),
        ]
        for mutate in mutations:
            source = row("xhttp")
            mutate(source)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                xray_outbound(source)

    def test_unsafe_and_unrecognized_options_are_refused(self):
        for changes in ({"address": "127.0.0.1"}, {"address": "localhost"}, {"port": True},
                        {"transport": "kcp"}, {"security": "none"},
                        {"securityOptions": {"serverName": "vpn.invalid", "allowInsecure": True}}):
            source = row()
            source.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                xray_outbound(source)


if __name__ == "__main__":
    unittest.main()
