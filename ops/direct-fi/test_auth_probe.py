"""Synthetic owner-only FI authentication probe contracts; no live I/O."""
import base64
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import socket
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


spec = importlib.util.spec_from_file_location("direct_fi_auth_probe_tests", Path(__file__).with_name("auth_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

PROFILE = "11111111-1111-4111-8111-111111111111"
NODE = "22222222-2222-4222-8222-222222222222"
INBOUNDS = ("33333333-3333-4333-8333-333333333333",
            "44444444-4444-4444-8444-444444444444",
            "55555555-5555-4555-8555-555555555555")
HOSTS = ("66666666-6666-4666-8666-666666666666",
         "77777777-7777-4777-8777-777777777777",
         "88888888-8888-4888-8888-888888888888")
SQUAD = "99999999-9999-4999-8999-999999999999"
USER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECOND_USER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
TAGS = ("ordinary-grpc-reality-fi", "ordinary-ws-fi", "vless-xhttp-yandex-fi")
ORIGIN = "fi103.wayspn.com"
CDN = "cdn-fi103.wayspn.com"
OWNER = 6910097562
PRIVATE = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
SHORT_ID = "0123456789abcdef"
EXTRA_SECRET = "PRIVATE-NOT-FOR-PROBE-OUTPUT"


def fixture():
    """Everything is synthetic, including the deterministic test-only key."""
    xhttp = {"mode": "packet-up", "path": "/assets/app/", "uplinkHTTPMethod": "GET",
             "uplinkDataPlacement": "header", "uplinkDataKey": "X-Transport-Data",
             "scMaxEachPostBytes": "2048", "serverMaxHeaderBytes": 8192}
    inbounds = [
        {"tag": TAGS[0], "protocol": "vless", "listen": "0.0.0.0", "port": 2083,
         "settings": {"clients": [], "decryption": "none"},
         "streamSettings": {"network": "grpc", "security": "reality",
             "grpcSettings": {"serviceName": "grpc"},
             "realitySettings": {"privateKey": PRIVATE, "shortIds": [SHORT_ID],
                                 "serverNames": [ORIGIN], "target": "127.0.0.1:443"}}},
        {"tag": TAGS[1], "protocol": "vless", "listen": "127.0.0.1", "port": 10088,
         "settings": {"clients": [], "decryption": "none"},
         "streamSettings": {"network": "ws", "security": "none", "wsSettings": {"path": "/direct/ws/"}}},
        {"tag": TAGS[2], "protocol": "vless", "listen": "127.0.0.1", "port": 10085,
         "settings": {"clients": [], "decryption": "none"},
         "streamSettings": {"network": "xhttp", "security": "none", "xhttpSettings": xhttp}},
    ]
    metadata = [{"uuid": iid, "tag": tag} for iid, tag in zip(INBOUNDS, TAGS)]
    profile = {"uuid": PROFILE, "name": "DIRECT_FI_102_103", "inbounds": metadata,
               "config": {"inbounds": inbounds, "outbounds": [{"protocol": "freedom"}],
                          "routing": {"rules": [{"type": "field", "outboundTag": "direct"}]}}}
    node = {"uuid": NODE, "name": "node-direct-fi-102.103", "address": "92.42.102.103", "port": 2222,
            "isConnected": True, "isDisabled": False, "lastStatusMessage": None,
            "configProfile": {"activeConfigProfileUuid": PROFILE, "activeInbounds": deepcopy(metadata)}}
    hosts = []
    for index, kind in enumerate(("grpc", "ws", "xhttp")):
        host = {"uuid": HOSTS[index], "remark": "Synthetic FI " + kind,
                "address": ("92.42.102.103", ORIGIN, CDN)[index],
                "port": 2083 if kind == "grpc" else 443, "sni": CDN if kind == "xhttp" else ORIGIN,
                "host": None if kind == "grpc" else CDN if kind == "xhttp" else ORIGIN,
                "path": ("grpc", "/direct/ws/", "/assets/app/")[index],
                "alpn": "http/1.1" if kind == "ws" else "h2", "fingerprint": "chrome",
                "securityLayer": "DEFAULT" if kind == "grpc" else "TLS",
                "isDisabled": False, "nodes": [NODE],
                "internalSquads": {"mode": "EXCLUDE", "squads": []},
                "inbound": {"configProfileUuid": PROFILE, "configProfileInboundUuid": INBOUNDS[index]}}
        if kind == "xhttp":
            host["xhttpExtraParams"] = {"uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header",
                "uplinkDataKey": "X-Transport-Data", "scMaxEachPostBytes": "1024-2048"}
        hosts.append(host)
    snapshot = {"profiles": [profile], "nodes": [node], "hosts": hosts,
                "squads": [{"uuid": SQUAD, "name": "Synthetic owner access",
                            "inbounds": [{"uuid": iid} for iid in INBOUNDS]}]}
    document = {"kind": "owner-only-lookup-not-subscription-config", "ownerTelegramId": OWNER,
                "users": [{"telegramId": OWNER, "status": "ACTIVE", "expireAt": "2999-01-01T00:00:00Z",
                           "vlessUuid": USER, "username": "owner-fi-test",
                           "activeInternalSquads": [{"uuid": SQUAD}]}]}
    return snapshot, document


def private_json(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.owner = fixture()

    def reject(self, change):
        snapshot, owner = deepcopy(self.snapshot), deepcopy(self.owner)
        change(snapshot, owner)
        with self.assertRaises(probe.ProbeError) as error:
            probe.build(snapshot, owner)
        self.assertNotIn(PRIVATE, str(error.exception))
        self.assertNotIn(USER, str(error.exception))
        self.assertNotIn(EXTRA_SECRET, str(error.exception))

    def test_exact_three_safe_client_outbounds_and_inputs_unchanged(self):
        original = deepcopy((self.snapshot, self.owner))
        jobs = probe.build(self.snapshot, self.owner)
        self.assertEqual((self.snapshot, self.owner), original)
        self.assertEqual([job["kind"] for job in jobs], ["grpc", "ws", "xhttp"])
        self.assertEqual([job["host_uuid"] for job in jobs], list(HOSTS))
        for job in jobs:
            outbound = job["outbound"]
            self.assertEqual(outbound["protocol"], "vless")
            self.assertEqual(len(outbound["settings"]["vnext"]), 1)
            self.assertEqual(outbound["settings"]["vnext"][0]["users"][0]["id"], USER)
            self.assertEqual(outbound["settings"]["vnext"][0]["users"][0]["encryption"], "none")
            self.assertNotIn("routing", outbound)
            self.assertNotIn("proxySettings", outbound)
        self.assertNotIn(PRIVATE, json.dumps(jobs))
        self.assertNotIn('"privateKey"', json.dumps(jobs))
        self.assertNotIn('"clients"', json.dumps(jobs))

    def test_grpc_maps_service_and_derives_public_key_not_server_key(self):
        grpc = probe.build(self.snapshot, self.owner)[0]["outbound"]
        server = grpc["settings"]["vnext"][0]
        stream = grpc["streamSettings"]
        public = X25519PrivateKey.from_private_bytes(bytes(range(32))).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        expected = base64.urlsafe_b64encode(public).decode().rstrip("=")
        self.assertEqual((server["address"], server["port"]), ("92.42.102.103", 2083))
        self.assertEqual(stream["network"], "grpc")
        self.assertEqual(stream["grpcSettings"]["serviceName"], "grpc")
        self.assertEqual(stream["security"], "reality")
        self.assertEqual(stream["realitySettings"]["publicKey"], expected)
        self.assertEqual(stream["realitySettings"]["serverName"], ORIGIN)
        self.assertEqual(stream["realitySettings"]["shortId"], SHORT_ID)

    def test_ws_and_xhttp_use_public_tls_hosts_not_loopback_origin(self):
        jobs = {row["kind"]: row["outbound"] for row in probe.build(self.snapshot, self.owner)}
        for kind, address, path, settings in (("ws", ORIGIN, "/direct/ws/", "wsSettings"),
                                              ("xhttp", CDN, "/assets/app/", "xhttpSettings")):
            stream = jobs[kind]["streamSettings"]
            server = jobs[kind]["settings"]["vnext"][0]
            self.assertEqual((server["address"], server["port"]), (address, 443))
            self.assertEqual(stream["security"], "tls")
            self.assertEqual(stream["tlsSettings"]["serverName"], address)
            self.assertIsNot(stream["tlsSettings"].get("allowInsecure"), True)
            self.assertEqual(stream[settings]["path"], path)
        xhttp = jobs["xhttp"]["streamSettings"]["xhttpSettings"]
        self.assertEqual(xhttp["mode"], "packet-up")
        self.assertEqual(xhttp["host"], CDN)
        for key, expected in self.snapshot["hosts"][2]["xhttpExtraParams"].items():
            self.assertEqual(xhttp["extra"][key], expected)
        self.assertNotIn("serverMaxHeaderBytes", xhttp)
        self.assertNotIn("serverMaxHeaderBytes", xhttp["extra"])

    def test_foreign_owner_wrapper_and_even_inactive_foreign_rows_rejected(self):
        changes = [lambda s, u: u.update(ownerTelegramId=42),
                   lambda s, u: u.update(kind="subscription-config"),
                   lambda s, u: u["users"][0].update(telegramId=42),
                   lambda s, u: u["users"].append({"telegramId": 42, "status": "DISABLED", "username": EXTRA_SECRET})]
        for change in changes:
            with self.subTest(change=change): self.reject(change)

    def test_missing_disabled_expired_and_invalid_vless_accounts_fail_closed(self):
        changes = [lambda s, u: u.update(users=[]),
                   lambda s, u: u["users"][0].update(status="DISABLED"),
                   lambda s, u: u["users"][0].update(expireAt="2000-01-01T00:00:00Z"),
                   lambda s, u: u["users"][0].update(expireAt="not-a-date"),
                   lambda s, u: u["users"][0].update(vlessUuid="not-a-uuid")]
        for change in changes:
            with self.subTest(change=change): self.reject(change)

    def test_ambiguous_accounts_require_exact_name_and_selected_account_is_active(self):
        second = deepcopy(self.owner["users"][0])
        second.update(vlessUuid=SECOND_USER, username="second-owner")
        self.owner["users"].append(second)
        with self.assertRaises(probe.ProbeError): probe.build(self.snapshot, self.owner)
        selected = probe.build(self.snapshot, self.owner, account_name="second-owner")
        self.assertTrue(all(row["outbound"]["settings"]["vnext"][0]["users"][0]["id"] == SECOND_USER for row in selected))
        for name in ("second", "SECOND-OWNER", "missing"):
            with self.subTest(name=name), self.assertRaises(probe.ProbeError):
                probe.build(self.snapshot, self.owner, account_name=name)
        second["status"] = "DISABLED"
        with self.assertRaises(probe.ProbeError):
            probe.build(self.snapshot, self.owner, account_name="second-owner")

    def test_scope_name_address_and_binding_drift_rejected(self):
        changes = [lambda s, u: s["profiles"][0].update(name="DIRECT_GERMANY"),
                   lambda s, u: s["nodes"][0].update(name="node-other"),
                   lambda s, u: s["nodes"][0].update(address="192.0.2.10"),
                   lambda s, u: s["nodes"][0]["configProfile"].update(activeConfigProfileUuid=SECOND_USER),
                   lambda s, u: s["hosts"][0]["inbound"].update(configProfileUuid=SECOND_USER),
                   lambda s, u: s["hosts"][0].update(nodes=[SECOND_USER]),
                   lambda s, u: s["hosts"][2].update(address="other.example.com")]
        for change in changes:
            with self.subTest(change=change): self.reject(change)

    def test_missing_duplicate_or_extra_target_inbounds_and_hosts_rejected(self):
        changes = [lambda s, u: s["profiles"].append(deepcopy(s["profiles"][0])),
                   lambda s, u: s["profiles"][0]["inbounds"].pop(),
                   lambda s, u: s["profiles"][0]["config"]["inbounds"].append(deepcopy(s["profiles"][0]["config"]["inbounds"][0])),
                   lambda s, u: s["nodes"][0]["configProfile"]["activeInbounds"].pop(),
                   lambda s, u: s["hosts"].pop(),
                   lambda s, u: s["hosts"].append({**deepcopy(s["hosts"][0]), "uuid": SECOND_USER})]
        for change in changes:
            with self.subTest(change=change): self.reject(change)

    def test_connection_disabled_and_error_guards_rejected(self):
        changes = [lambda s, u: s["nodes"][0].update(isConnected=False),
                   lambda s, u: s["nodes"][0].update(isDisabled=True),
                   lambda s, u: s["nodes"][0].update(lastStatusMessage=EXTRA_SECRET),
                   lambda s, u: s["hosts"][0].update(isDisabled=True),
                   lambda s, u: s["hosts"][1].update(isDisabled=True),
                   lambda s, u: s["hosts"][2].update(isDisabled=True)]
        for change in changes:
            with self.subTest(change=change): self.reject(change)

    def test_every_inbound_requires_active_user_squad_access(self):
        changes = [lambda s, u: u["users"][0].update(activeInternalSquads=[]),
                   lambda s, u: s["squads"][0]["inbounds"].pop(),
                   lambda s, u: s.update(squads=[]),
                   lambda s, u: s["hosts"][0].update(internalSquads={"mode": "INCLUDE", "squads": [SQUAD]}),
                   lambda s, u: s["hosts"][2].update(internalSquads={"mode": "EXCLUDE", "squads": [SQUAD]})]
        for change in changes:
            with self.subTest(change=change): self.reject(change)

    def test_separate_active_squads_may_cover_three_inbounds(self):
        second_squad = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        self.snapshot["squads"][0]["inbounds"] = [{"uuid": INBOUNDS[0]}]
        self.snapshot["squads"].append({"uuid": second_squad, "inbounds": [{"uuid": iid} for iid in INBOUNDS[1:]]})
        self.owner["users"][0]["activeInternalSquads"].append({"uuid": second_squad})
        self.assertEqual(len(probe.build(self.snapshot, self.owner)), 3)

    def test_grpc_ws_and_xhttp_transport_drift_rejected(self):
        changes = [lambda s, u: s["profiles"][0]["config"]["inbounds"][0]["streamSettings"]["grpcSettings"].update(serviceName="wrong"),
                   lambda s, u: s["profiles"][0]["config"]["inbounds"][0]["streamSettings"]["realitySettings"].update(privateKey="invalid"),
                   lambda s, u: s["profiles"][0]["config"]["inbounds"][0]["streamSettings"]["realitySettings"].update(shortIds=["wrong"]),
                   lambda s, u: s["profiles"][0]["config"]["inbounds"][1].update(listen="0.0.0.0"),
                   lambda s, u: s["profiles"][0]["config"]["inbounds"][2].update(listen="0.0.0.0"),
                   lambda s, u: s["hosts"][0].update(path="not-grpc"),
                   lambda s, u: s["hosts"][1].update(path="/wrong/"),
                   lambda s, u: s["hosts"][2]["xhttpExtraParams"].update(uplinkHTTPMethod="POST"),
                   lambda s, u: s["hosts"][2]["xhttpExtraParams"].update(scMaxEachPostBytes="4096"),
                   lambda s, u: s["profiles"][0]["config"]["inbounds"][2]["streamSettings"]["xhttpSettings"].update(mode="stream-up"),
                   lambda s, u: s["profiles"][0]["config"]["inbounds"][1]["settings"].update(clients=[{"id": USER}])]
        for change in changes:
            with self.subTest(change=change): self.reject(change)

    def test_arbitrary_server_and_host_fields_are_not_copied_to_clients(self):
        self.snapshot["profiles"][0]["config"]["inbounds"][0]["streamSettings"]["realitySettings"]["serverOnlyNote"] = EXTRA_SECRET
        for host in self.snapshot["hosts"]: host["unrelatedPrivateMetadata"] = EXTRA_SECRET
        jobs = probe.build(self.snapshot, self.owner)
        self.assertNotIn(EXTRA_SECRET, json.dumps(jobs))

    def test_xhttp_unknown_and_dependent_extra_fields_are_rejected(self):
        for key, value in (("privateKey", EXTRA_SECRET), ("downloadSettings", {"address": "other.example.com"}),
                           ("dialerProxy", "foreign-outbound"), ("unreviewedOption", True)):
            with self.subTest(key=key):
                self.reject(lambda s, u: s["hosts"][2]["xhttpExtraParams"].update({key: value}))


class PrivatePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fi-auth-offline-"))
        self.directory = Path(self.temporary)
        self.directory.chmod(0o700)
        self.snapshot, self.owner = fixture()
        self.snapshot_path, self.owner_path = self.directory / "snapshot.json", self.directory / "owner.json"
        private_json(self.snapshot_path, self.snapshot)
        private_json(self.owner_path, self.owner)
        self.state = self.directory / "state"

    def test_default_main_is_pure_no_file_or_network_access(self):
        with patch.object(probe, "prepare") as prepare, patch.object(probe, "run") as run, \
             patch("builtins.open", side_effect=AssertionError("unexpected file read")), \
             patch.object(Path, "open", side_effect=AssertionError("unexpected file read")), \
             patch.object(socket, "create_connection", side_effect=AssertionError("unexpected network")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(probe.main([]), 0)
        prepare.assert_not_called(); run.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        self.assertNotIn(USER, output.getvalue())
        self.assertFalse(self.state.exists())

    def test_prepare_requires_owner_confirmation_before_creating_state(self):
        with self.assertRaises(probe.ProbeError):
            probe.prepare(self.snapshot_path, self.owner_path, self.state)
        self.assertFalse(self.state.exists())

    def test_prepare_writes_only_private_bundle_and_returns_redacted_result(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            result = probe.prepare(self.snapshot_path, self.owner_path, self.state, owner_confirmed=True)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        bundle = self.state / "bundle.private.json"
        self.assertTrue(bundle.is_file())
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o600)
        self.assertEqual(bundle.stat().st_nlink, 1)
        text = output.getvalue() + json.dumps(result)
        for secret in (USER, PRIVATE, SHORT_ID, EXTRA_SECRET): self.assertNotIn(secret, text)
        self.assertNotIn(PRIVATE, bundle.read_text())

    def test_prepare_rejects_public_or_symlinked_owner_input(self):
        self.owner_path.chmod(0o644)
        with self.assertRaises(probe.ProbeError):
            probe.prepare(self.snapshot_path, self.owner_path, self.state, owner_confirmed=True)
        self.assertFalse(self.state.exists())
        self.owner_path.chmod(0o600)
        link = self.directory / "owner-link.json"; link.symlink_to(self.owner_path)
        with self.assertRaises(probe.ProbeError):
            probe.prepare(self.snapshot_path, link, self.state, owner_confirmed=True)
        self.assertFalse(self.state.exists())

    def test_prepare_never_overwrites_an_existing_bundle(self):
        probe.prepare(self.snapshot_path, self.owner_path, self.state, owner_confirmed=True)
        original = (self.state / "bundle.private.json").read_bytes()
        with self.assertRaises(probe.ProbeError):
            probe.prepare(self.snapshot_path, self.owner_path, self.state, owner_confirmed=True)
        self.assertEqual((self.state / "bundle.private.json").read_bytes(), original)

    def test_run_requires_confirmation_before_loading_or_launching_engine(self):
        with patch.object(probe, "pinned_engine") as pinned, patch.object(probe, "test_one") as test_one:
            with self.assertRaises(probe.ProbeError):
                probe.run(self.state, self.directory / "xray", "0" * 64)
        pinned.assert_not_called(); test_one.assert_not_called()
        self.assertFalse(self.state.exists())


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(dir="/private/tmp", prefix="fi-auth-isolation-"))
        self.directory = Path(self.temporary)
        self.directory.chmod(0o700)
        self.snapshot, self.owner = fixture()
        self.jobs = probe.build(self.snapshot, self.owner)
        self.snapshot_path, self.owner_path = self.directory / "snapshot.json", self.directory / "owner.json"
        private_json(self.snapshot_path, self.snapshot)
        private_json(self.owner_path, self.owner)
        self.state = self.directory / "state"

    def test_single_config_has_one_loopback_socks_listener_and_no_fallback(self):
        for row in self.jobs:
            with self.subTest(kind=row["kind"]):
                original = deepcopy(row["outbound"])
                result = probe.single_config(row["outbound"], 23456)
                self.assertEqual(row["outbound"], original)
                self.assertEqual(len(result["outbounds"]), 1)
                self.assertEqual(result["outbounds"][0]["tag"], "audit-selected")
                self.assertEqual(result["outbounds"][0]["settings"], original["settings"])
                self.assertEqual(result["inbounds"], [{"tag": "audit-socks", "listen": "127.0.0.1", "port": 23456,
                    "protocol": "socks", "settings": {"auth": "noauth", "udp": False}}])
                self.assertNotIn("dns", result)
                self.assertNotIn("balancers", result["routing"])
                self.assertEqual(result["routing"]["rules"], [{"type": "field", "inboundTag": ["audit-socks"],
                                                               "outboundTag": "audit-selected"}])
                self.assertNotIn('"freedom"', json.dumps(result))
                self.assertNotIn('"direct"', json.dumps(result))

    def test_single_config_rejects_extra_proxy_chain_routing_and_insecure_tls(self):
        for key, value in (("proxySettings", {"tag": "other"}), ("sendThrough", "192.0.2.1"),
                           ("mux", {"enabled": True}), ("routing", {"rules": []})):
            outbound = deepcopy(self.jobs[0]["outbound"])
            outbound[key] = value
            with self.subTest(key=key), self.assertRaises(probe.ProbeError):
                probe.single_config(outbound, 23456)
        outbound = deepcopy(self.jobs[1]["outbound"])
        outbound["streamSettings"]["tlsSettings"]["allowInsecure"] = True
        with self.assertRaises(probe.ProbeError): probe.single_config(outbound, 23456)

    def reservation(self):
        reservation = MagicMock()
        reservation.__enter__.return_value = reservation
        reservation.getsockname.return_value = ("127.0.0.1", 23456)
        return reservation

    def test_mocked_authenticated_https_keeps_credentials_out_of_argv_and_cleans_config(self):
        engine = self.directory / "xray"
        child = Mock()
        child.poll.return_value = None
        seen_config = []
        def execute(args, **kwargs):
            self.assertEqual(kwargs["env"], probe.ENV)
            self.assertEqual(kwargs["cwd"], self.directory)
            for secret in (USER, PRIVATE, SHORT_ID): self.assertNotIn(secret, json.dumps(args))
            if args[0] == str(engine):
                self.assertEqual(args[1:3], ["run", "-test"])
                config_path = Path(args[args.index("-config") + 1])
                self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
                document = json.loads(config_path.read_text())
                self.assertEqual(len(document["outbounds"]), 1)
                self.assertEqual(document["outbounds"][0]["settings"]["vnext"][0]["users"][0]["id"], USER)
                seen_config.append(config_path)
                return SimpleNamespace(returncode=0)
            self.assertEqual(args[0], probe.CURL)
            self.assertIn("--socks5-hostname", args)
            self.assertEqual(args[-1], "https://www.gstatic.com/generate_204")
            self.assertIn("--tlsv1.2", args)
            self.assertNotIn("--insecure", args)
            self.assertNotIn("-k", args)
            return SimpleNamespace(returncode=0, stdout=b"204 0.123")
        with patch.object(probe.socket, "socket", return_value=self.reservation()), \
             patch.object(probe.socket, "create_connection", return_value=MagicMock()) as connect, \
             patch.object(probe.subprocess, "run", side_effect=execute) as execute_mock, \
             patch.object(probe.subprocess, "Popen", return_value=child) as launch, \
             patch.object(probe, "stop") as stop:
            result = probe.test_one(engine, self.jobs[0]["outbound"], self.directory, 0)
        self.assertEqual(result, {"status": "passed", "reason": "https_204", "elapsedMs": 123})
        self.assertEqual(execute_mock.call_count, 2)
        self.assertEqual(connect.call_args.args, (("127.0.0.1", 23456),))
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        stop.assert_called_once_with(child)
        self.assertEqual(len(seen_config), 1)
        self.assertFalse(seen_config[0].exists())
        self.assertNotIn(USER, json.dumps(result))
        for path in self.directory.iterdir(): self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_engine_rejection_is_uncovered_without_launch_or_network(self):
        with patch.object(probe.socket, "socket", return_value=self.reservation()), \
             patch.object(probe.socket, "create_connection") as connect, \
             patch.object(probe.subprocess, "run", return_value=SimpleNamespace(returncode=1)), \
             patch.object(probe.subprocess, "Popen") as launch, patch.object(probe, "stop"):
            result = probe.test_one(self.directory / "xray", self.jobs[0]["outbound"], self.directory, 0)
        self.assertEqual(result, {"status": "uncovered", "reason": "engine_config_rejected"})
        connect.assert_not_called(); launch.assert_not_called()
        self.assertFalse((self.directory / "config-0.json").exists())

    def test_arbitrary_local_exception_is_redacted_and_credential_config_removed(self):
        with patch.object(probe.socket, "socket", return_value=self.reservation()), \
             patch.object(probe.subprocess, "run", side_effect=RuntimeError(USER + EXTRA_SECRET)), \
             patch.object(probe.subprocess, "Popen") as launch, patch.object(probe, "stop"):
            result = probe.test_one(self.directory / "xray", self.jobs[0]["outbound"], self.directory, 0)
        self.assertEqual(result, {"status": "uncovered", "reason": "local_test_error"})
        self.assertNotIn(USER, json.dumps(result)); self.assertNotIn(EXTRA_SECRET, json.dumps(result))
        self.assertFalse((self.directory / "config-0.json").exists())
        launch.assert_not_called()

    def test_run_checks_exactly_three_isolated_outbounds_and_redacts_summary(self):
        probe.prepare(self.snapshot_path, self.owner_path, self.state, owner_confirmed=True)
        engine = self.directory / "xray"
        seen = []
        def fake_test_one(actual_engine, outbound, directory, number):
            self.assertEqual(actual_engine, engine)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertTrue(directory.is_relative_to(self.state))
            self.assertEqual(outbound["settings"]["vnext"][0]["users"][0]["id"], USER)
            seen.append((outbound["streamSettings"]["network"], number))
            return {"status": "passed", "reason": "https_204", "elapsedMs": 12}
        with patch.object(probe, "pinned_engine") as pinned, \
             patch.object(probe, "test_one", side_effect=fake_test_one), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            result = probe.run(self.state, engine, "0" * 64, owner_confirmed=True)
        pinned.assert_called_once_with(engine, "0" * 64)
        self.assertEqual([network for network, _ in seen], ["grpc", "ws", "xhttp"])
        self.assertEqual(len({number for _, number in seen}), 3)
        self.assertEqual(result["counts"], {"passed": 3, "failed": 0, "uncovered": 0})
        self.assertTrue(result["completed"])
        self.assertTrue(result["all_passed"])
        text = output.getvalue() + json.dumps(result)
        for secret in (USER, PRIVATE, SHORT_ID, *HOSTS): self.assertNotIn(secret, text)
        results_path = Path(result["results"])
        self.assertEqual(stat.S_IMODE(results_path.stat().st_mode), 0o600)
        self.assertNotIn(USER, results_path.read_text())
        self.assertNotIn(PRIVATE, results_path.read_text())

    def test_completed_run_with_uncovered_or_failed_probe_is_not_success(self):
        probe.prepare(self.snapshot_path, self.owner_path, self.state, owner_confirmed=True)
        for status in ("uncovered", "failed"):
            rows = [{"status": "passed", "reason": "https_204", "elapsedMs": 12},
                    {"status": "passed", "reason": "https_204", "elapsedMs": 12},
                    {"status": status, "reason": "engine_config_rejected" if status == "uncovered" else "https_probe_failed"}]
            with self.subTest(status=status), patch.object(probe, "pinned_engine"), \
                 patch.object(probe, "test_one", side_effect=rows):
                result = probe.run(self.state, self.directory / "xray", "0" * 64, owner_confirmed=True)
            self.assertTrue(result["completed"])
            self.assertFalse(result["all_passed"])
            self.assertEqual(result["counts"][status], 1)
            self.assertEqual(sum(result["counts"].values()), 3)

    def test_tampered_private_bundle_stops_before_any_authenticated_probe(self):
        probe.prepare(self.snapshot_path, self.owner_path, self.state, owner_confirmed=True)
        bundle_path = self.state / "bundle.private.json"
        value = json.loads(bundle_path.read_text())
        value["probes"][0]["outbound"]["settings"]["vnext"][0]["address"] = "other.example.com"
        private_json(bundle_path, value)
        with patch.object(probe, "pinned_engine") as pinned, patch.object(probe, "test_one") as test_one:
            with self.assertRaises(probe.ProbeError):
                probe.run(self.state, self.directory / "xray", "0" * 64, owner_confirmed=True)
        pinned.assert_not_called(); test_one.assert_not_called()


if __name__ == "__main__": unittest.main()
