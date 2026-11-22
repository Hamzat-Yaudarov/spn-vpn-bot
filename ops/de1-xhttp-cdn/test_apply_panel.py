"""Keyless unittest contracts: fake panel only, no .env, subscribers or SSH."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


origin = _load("de1_apply_test_origin", HERE / "prepare.py")
with patch.dict(sys.modules, {"prepare": origin}):
    plan = _load("de1_apply_test_plan", HERE / "panel_plan.py")
with patch.dict(sys.modules, {"prepare": origin, "panel_plan": plan}):
    apply = _load("de1_apply_test_helper", HERE / "apply_panel.py")


OLD_INBOUND = "synthetic-old-inbound"
DE_SQUAD = "synthetic-de-squad"
NEW_PROFILE = "synthetic-created-profile"
NEW_INBOUND = "synthetic-created-inbound"
NEW_NODE = "synthetic-created-node"
NEW_HOST = "synthetic-created-host"


def _row(rows, identity):
    return next(item for item in rows if item["uuid"] == identity)


def baseline():
    return {
        "profiles": [{"uuid": plan.SOURCE_PROFILE, "name": "synthetic-source",
            "config": {"inbounds": [{"tag": apply.SOURCE_TAG, "settings": {"clients": []}}],
                       "syntheticPrivateField": "SYNTHETIC-NOT-A-REAL-SECRET"},
            "inbounds": [{"uuid": OLD_INBOUND, "tag": apply.SOURCE_TAG}]}],
        "nodes": [{"uuid": "synthetic-old-node", "name": "synthetic-old-node",
            "address": "192.0.2.10", "port": 2222, "isConnected": True,
            "configProfile": {"activeConfigProfileUuid": plan.SOURCE_PROFILE,
                              "activeInbounds": [{"uuid": OLD_INBOUND}]}}],
        "hosts": [{"uuid": plan.SOURCE_HOST, "remark": "synthetic-source",
            "address": "source.example.test", "port": 443, "securityLayer": "TLS",
            "sni": "source.example.test", "host": "source.example.test", "path": "/synthetic-source/",
            "alpn": "h2", "fingerprint": "chrome", "isDisabled": False,
            "xhttpExtraParams": {"syntheticSourceExtra": True},
            "inbound": {"configProfileUuid": plan.SOURCE_PROFILE, "configProfileInboundUuid": OLD_INBOUND},
            "nodes": ["synthetic-old-node"], "internalSquads": {"mode": "EXCLUDE", "squads": []}}],
        "squads": [{"uuid": DE_SQUAD, "name": "DE", "inbounds": [{"uuid": OLD_INBOUND}]},
                   {"uuid": "synthetic-public-squad", "name": "PUBLIC", "inbounds": [{"uuid": OLD_INBOUND}]}],
    }


class FakePanel:
    """Independent server state; strict API allowlist and controllable lost replies."""
    def __init__(self, initial, payloads):
        self.live = copy.deepcopy(initial)
        self.initial = copy.deepcopy(initial)
        self.payloads = copy.deepcopy(payloads)
        self.calls = []
        self.fail_once = None
        self.no_commit_once = None
        self.after_write = None
        self.returned_inbound = NEW_INBOUND
        self.domain = payloads["host"]["address"]

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def expected(self, kind):
        result = copy.deepcopy(self.payloads[kind])
        if kind == "node":
            result["configProfile"] = {"activeConfigProfileUuid": NEW_PROFILE, "activeInbounds": [self.returned_inbound]}
        elif kind == "host":
            result.update(address=self.domain, sni=self.domain, host=self.domain)
            result["nodes"] = [NEW_NODE]
            result["inbound"] = {"configProfileUuid": NEW_PROFILE, "configProfileInboundUuid": self.returned_inbound}
        return result

    def api(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET":
            if body is not None or path not in {"/config-profiles", "/nodes", "/hosts", "/internal-squads"}:
                raise AssertionError("Forbidden read/subscriber endpoint")
            key = {"/config-profiles": "profiles", "/nodes": "nodes", "/hosts": "hosts", "/internal-squads": "squads"}[path]
            result = copy.deepcopy(self.live[key])
            return {"configProfiles": result} if key == "profiles" else {"internalSquads": result} if key == "squads" else result
        if method == "POST":
            kinds = {"/config-profiles": ("profile", "profiles", NEW_PROFILE),
                     "/nodes": ("node", "nodes", NEW_NODE), "/hosts": ("host", "hosts", NEW_HOST)}
            if path not in kinds:
                raise AssertionError("Forbidden creation endpoint")
            kind, collection, identity = kinds[path]
            if body != self.expected(kind):
                raise AssertionError("Creation payload exceeds the approved plan")
            if any(row["uuid"] == identity for row in self.live[collection]):
                raise AssertionError("Blind duplicate POST")
        elif method == "PATCH":
            if path == "/internal-squads":
                if body != {"uuid": DE_SQUAD, "inbounds": [OLD_INBOUND, NEW_INBOUND]}:
                    raise AssertionError("Squad PATCH exceeds append-only DE scope")
            elif path == "/hosts":
                if body != {"uuid": NEW_HOST, "isDisabled": False}:
                    raise AssertionError("Host PATCH exceeds owned-host enable scope")
            else:
                raise AssertionError("Existing profiles/nodes must never be patched")
        else:
            raise AssertionError("Forbidden mutation method")
        failure = self.fail_once if self.fail_once and self.fail_once[:2] == (method, path) else None
        if failure:
            self.fail_once = None
            if failure[2] == "before":
                raise TimeoutError("synthetic lost response")
        commit = self.no_commit_once != (method, path)
        if not commit:
            self.no_commit_once = None
        if commit and method == "POST":
            created = copy.deepcopy(body)
            created["uuid"] = identity
            if kind == "profile":
                created["inbounds"] = [{"uuid": self.returned_inbound, "tag": plan.TAG}]
            elif kind == "node":
                created["configProfile"]["activeInbounds"] = [{"uuid": value} for value in body["configProfile"]["activeInbounds"]]
                created.update(isConnected=False, isDisabled=False, lastStatusMessage=None)
            self.live[collection].append(created)
        elif commit and path == "/internal-squads":
            _row(self.live["squads"], DE_SQUAD)["inbounds"] = [{"uuid": value} for value in body["inbounds"]]
        elif commit:
            _row(self.live["hosts"], NEW_HOST)["isDisabled"] = False
        if self.after_write:
            self.after_write(method, path, self.live)
        if failure:
            raise TimeoutError("synthetic lost response")
        return {}


class PanelApplyContracts(unittest.TestCase):
    def setUp(self):
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="de1-apply-unit-", dir="/private/tmp")))
        self.stage = origin.prepare(root / "origin")
        self.original = baseline()
        artifacts = plan.build_plan(plan.metadata_snapshot(self.original), self.stage, "cdn-de1.chislo.site")
        self.plan_dir = root / "plan"
        plan.private_directory(self.plan_dir)
        for name, value in artifacts.items():
            plan.write(self.plan_dir / name, value)
        self.state_dir = root / "apply-state"
        self.panel = FakePanel(self.original, apply.load_plan(self.plan_dir))
        self.enterContext(patch.object(apply, "api", self.panel.api))

    def core(self):
        return apply.create_core(self.plan_dir, self.state_dir, confirm=True)

    def host(self, **kwargs):
        self.core()
        return apply.create_host(self.plan_dir, self.state_dir, confirm=True, **kwargs)

    def healthy_host(self):
        state = self.host()
        _row(self.panel.live["nodes"], NEW_NODE)["isConnected"] = True
        self.panel.calls.clear()
        return state

    def assert_readonly(self):
        self.assertEqual(self.panel.writes, [])

    def test_confirmation_is_literal_and_checked_before_api(self):
        for value in (False, None, 1, "yes"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                apply.create_core(self.plan_dir, self.state_dir, confirm=value)
        self.assertEqual(self.panel.calls, [])

    def test_core_only_posts_exact_new_profile_and_node_and_preserves_baseline(self):
        state = self.core()
        self.assertEqual(self.panel.writes, [("POST", "/config-profiles", self.panel.expected("profile")),
                                           ("POST", "/nodes", self.panel.expected("node"))])
        self.assertEqual(state["owned"], {"profile": NEW_PROFILE, "inbound": NEW_INBOUND, "node": NEW_NODE})
        self.assertEqual(self.panel.live["hosts"], self.original["hosts"])
        self.assertEqual(self.panel.live["squads"], self.original["squads"])
        self.assertEqual(_row(self.panel.live["profiles"], plan.SOURCE_PROFILE), self.original["profiles"][0])
        self.assertEqual(_row(self.panel.live["nodes"], "synthetic-old-node"), self.original["nodes"][0])
        self.panel.calls.clear()
        self.core()
        apply.audit(self.state_dir)
        self.assert_readonly()

    def test_manifest_is_private_and_contains_hashes_not_source_configs_or_credentials(self):
        self.core()
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
        for path in self.state_dir.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        content = (self.state_dir / "manifest.json").read_text()
        self.assertNotIn("SYNTHETIC-NOT-A-REAL-SECRET", content)
        self.assertNotIn("syntheticPrivateField", content)
        self.assertNotIn("source.example.test", content)
        self.assertEqual(json.loads(content)["baseline"], apply.signatures(self.original))

    def test_no_subscriber_or_keygen_endpoints_are_ever_used(self):
        self.healthy_host()
        apply.enable_de(self.state_dir, confirm=True, cdn_verified=True)
        self.assertTrue(all(path in {"/config-profiles", "/nodes", "/hosts", "/internal-squads"}
                            for _, path, _ in self.panel.calls))
        self.assertFalse(any(method == "PATCH" and path in {"/config-profiles", "/nodes"}
                             for method, path, _ in self.panel.calls))

    def test_lost_core_post_responses_resume_without_duplicate_creation(self):
        for endpoint in ("/config-profiles", "/nodes"):
            for when in ("before", "after"):
                with self.subTest(endpoint=endpoint, when=when):
                    # Each subcase needs a wholly separate server/manifest.
                    self.panel.fail_once = ("POST", endpoint, when)
                    state_dir = self.state_dir.with_name("state-" + endpoint.strip("/") + "-" + when)
                    self.panel.live = copy.deepcopy(self.original)
                    self.panel.calls.clear()
                    with self.assertRaises(TimeoutError):
                        apply.create_core(self.plan_dir, state_dir, confirm=True)
                    writes_before = len(self.panel.writes)
                    self.assertEqual(sum(path == endpoint for _, path, _ in self.panel.writes), 1)
                    self.panel.calls.clear()
                    state = apply.create_core(self.plan_dir, state_dir, confirm=True)
                    self.assertEqual(state["owned"]["node"], NEW_NODE)
                    expected = (["/config-profiles", "/nodes"] if endpoint == "/config-profiles" and when == "before"
                                else ["/nodes"] if endpoint == "/config-profiles" or when == "before" else [])
                    self.assertEqual([path for _, path, _ in self.panel.writes], expected)
                    self.assertGreater(writes_before, 0)
                    self.assertEqual([method for method, _, _ in self.panel.calls[:4]], ["GET"] * 4)

    def test_success_response_without_profile_commit_stops_before_node(self):
        self.panel.no_commit_once = ("POST", "/config-profiles")
        with self.assertRaises(ValueError):
            self.core()
        self.assertEqual([path for _, path, _ in self.panel.writes], ["/config-profiles"])
        self.panel.calls.clear()
        self.core()
        self.assertEqual([path for _, path, _ in self.panel.writes], ["/config-profiles", "/nodes"])

    def test_existing_named_profile_without_manifest_is_not_adopted(self):
        existing = {"uuid": NEW_PROFILE, **self.panel.expected("profile"),
                    "inbounds": [{"uuid": NEW_INBOUND, "tag": plan.TAG}]}
        self.panel.live["profiles"].append(existing)
        with self.assertRaises(ValueError):
            self.core()
        self.assert_readonly()

    def test_conflicting_lost_response_object_blocks_resume(self):
        self.panel.fail_once = ("POST", "/config-profiles", "after")
        with self.assertRaises(TimeoutError):
            self.core()
        _row(self.panel.live["profiles"], NEW_PROFILE)["config"]["log"] = {"loglevel": "debug"}
        self.panel.calls.clear()
        with self.assertRaises(ValueError):
            self.core()
        self.assert_readonly()

    def test_created_inbound_must_not_alias_any_existing_inbound(self):
        self.panel.returned_inbound = OLD_INBOUND
        with self.assertRaises(ValueError):
            self.core()
        self.assertEqual([path for _, path, _ in self.panel.writes], ["/config-profiles"])

    def test_profile_template_cannot_add_proxy_or_cascade_outbound(self):
        path = self.plan_dir / "profile.create.json"
        value = json.loads(path.read_text())
        value["config"]["outbounds"].append({"tag": "unexpected-cascade", "protocol": "socks"})
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            self.core()
        self.assertEqual(self.panel.calls, [])

    def test_unapproved_node_address_or_delivery_domain_is_rejected(self):
        for filename, field, value in (("node.create.template.json", "address", "192.0.2.99"),
                                       ("host.create.template.json", "address", "unapproved.example.test")):
            path = self.plan_dir / filename
            original = path.read_text()
            data = json.loads(original)
            data[field] = value
            path.write_text(json.dumps(data))
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.core()
            path.write_text(original)
        self.assertEqual(self.panel.calls, [])

    def test_changed_source_tag_binding_stops_core_before_post(self):
        self.panel.live["profiles"][0]["inbounds"][0]["tag"] = "unexpected-source-tag"
        with self.assertRaises(ValueError):
            self.core()
        self.assert_readonly()

    def test_source_host_whole_object_drift_is_not_hidden_by_old_projection(self):
        self.core()
        self.panel.calls.clear()
        self.panel.live["hosts"][0]["sni"] = "changed.example.test"
        with self.assertRaises(ValueError):
            apply.audit(self.state_dir)
        self.assert_readonly()

    def test_existing_node_telemetry_is_not_binding_drift(self):
        self.core()
        self.panel.calls.clear()
        self.panel.live["nodes"][0]["isConnected"] = False
        apply.audit(self.state_dir)
        self.assert_readonly()

    def test_between_profile_and_node_existing_drift_stops_node_post(self):
        def drift(method, path, live):
            if path == "/config-profiles":
                live["hosts"][0]["path"] = "/changed-existing/"
        self.panel.after_write = drift
        with self.assertRaises(ValueError):
            self.core()
        self.assertEqual([path for _, path, _ in self.panel.writes], ["/config-profiles"])

    def test_disabled_host_is_exactly_owned_and_de_allow_only(self):
        state = self.host()
        host = _row(self.panel.live["hosts"], NEW_HOST)
        self.assertEqual(state["owned"]["host"], NEW_HOST)
        self.assertIs(host["isDisabled"], True)
        self.assertEqual(host["internalSquads"], {"mode": "ALLOW_ONLY", "squads": [DE_SQUAD]})
        self.assertEqual(host["nodes"], [NEW_NODE])
        self.assertEqual(self.panel.live["squads"], self.original["squads"])
        self.assertEqual([path for _, path, _ in self.panel.writes], ["/config-profiles", "/nodes", "/hosts"])
        self.panel.calls.clear()
        apply.create_host(self.plan_dir, self.state_dir, confirm=True)
        self.assert_readonly()

    def test_approved_technical_domain_changes_only_host_endpoint_fields(self):
        self.core()
        technical = "1sd2ljvb74.cdn.twcstorage.ru"
        self.panel.domain = technical
        apply.create_host(self.plan_dir, self.state_dir, confirm=True, cdn_domain=technical)
        host = _row(self.panel.live["hosts"], NEW_HOST)
        self.assertEqual((host["address"], host["sni"], host["host"]), (technical,) * 3)
        self.assertEqual(host["path"], self.panel.payloads["host"]["path"])
        self.assertIs(host["isDisabled"], True)

    def test_host_confirmation_and_unknown_domain_fail_before_writes(self):
        self.core()
        self.panel.calls.clear()
        with self.assertRaises(ValueError):
            apply.create_host(self.plan_dir, self.state_dir)
        with self.assertRaises(ValueError):
            apply.create_host(self.plan_dir, self.state_dir, confirm=True, cdn_domain="unapproved.example.test")
        self.assert_readonly()

    def test_lost_disabled_host_response_is_adopted_only_exactly(self):
        self.core()
        self.panel.fail_once = ("POST", "/hosts", "after")
        with self.assertRaises(TimeoutError):
            apply.create_host(self.plan_dir, self.state_dir, confirm=True)
        self.panel.calls.clear()
        apply.create_host(self.plan_dir, self.state_dir, confirm=True)
        self.assert_readonly()

    def test_unknown_host_endpoint_collision_stops_without_creation(self):
        self.core()
        collision = copy.deepcopy(self.original["hosts"][0])
        collision.update(uuid="synthetic-collision", address=self.panel.domain, remark="unrelated-host")
        self.panel.live["hosts"].append(collision)
        self.panel.calls.clear()
        with self.assertRaises(ValueError):
            apply.create_host(self.plan_dir, self.state_dir, confirm=True)
        self.assert_readonly()

    def test_saved_host_manifest_cannot_expand_scope(self):
        self.host()
        path = self.state_dir / "manifest.json"
        original = path.read_text()
        for field, value in (("path", "/unexpected/"), ("address", "unapproved.example.test"),
                             ("internalSquads", {"mode": "EXCLUDE", "squads": []}),
                             ("xrayJsonTemplateUuid", "unexpected-template")):
            saved = json.loads(original)
            saved["host"][field] = value
            path.write_text(json.dumps(saved))
            self.panel.calls.clear()
            with self.subTest(field=field), self.assertRaises(ValueError):
                apply.audit(self.state_dir)
            self.assertEqual(self.panel.calls, [])
        path.write_text(original)

    def test_activation_request_flag_must_be_literal_bool(self):
        self.host()
        path = self.state_dir / "manifest.json"
        original = path.read_text()
        for value in (1, "true", None):
            saved = json.loads(original)
            saved["activationRequested"] = value
            path.write_text(json.dumps(saved))
            self.panel.calls.clear()
            with self.subTest(value=value), self.assertRaises(ValueError):
                apply.audit(self.state_dir)
            self.assertEqual(self.panel.calls, [])
        path.write_text(original)

    def test_activation_requires_both_explicit_confirmations(self):
        self.healthy_host()
        for kwargs in ({}, {"confirm": True}, {"cdn_verified": True}, {"confirm": 1, "cdn_verified": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                apply.enable_de(self.state_dir, **kwargs)
        self.assertEqual(self.panel.calls, [])

    def test_activation_appends_only_owned_inbound_and_enables_only_owned_host(self):
        self.healthy_host()
        apply.enable_de(self.state_dir, confirm=True, cdn_verified=True)
        self.assertEqual(self.panel.writes, [
            ("PATCH", "/internal-squads", {"uuid": DE_SQUAD, "inbounds": [OLD_INBOUND, NEW_INBOUND]}),
            ("PATCH", "/hosts", {"uuid": NEW_HOST, "isDisabled": False}),
        ])
        self.assertEqual(_row(self.panel.live["squads"], "synthetic-public-squad"), self.original["squads"][1])
        self.assertEqual(_row(self.panel.live["hosts"], plan.SOURCE_HOST), self.original["hosts"][0])
        self.panel.calls.clear()
        apply.enable_de(self.state_dir, confirm=True, cdn_verified=True)
        self.assert_readonly()

    def test_unhealthy_node_prevents_activation(self):
        self.healthy_host()
        node = _row(self.panel.live["nodes"], NEW_NODE)
        for field, value in (("isConnected", False), ("isDisabled", True), ("lastStatusMessage", "synthetic-status-error")):
            before = copy.deepcopy(node)
            node[field] = value
            self.panel.calls.clear()
            with self.subTest(field=field), self.assertRaises(ValueError):
                apply.enable_de(self.state_dir, confirm=True, cdn_verified=True)
            self.assert_readonly()
            node.clear()
            node.update(before)

    def test_freshly_observed_unhealthy_node_after_squad_append_blocks_host_enable(self):
        self.healthy_host()
        def drift(method, path, live):
            if method == "PATCH" and path == "/internal-squads":
                _row(live["nodes"], NEW_NODE)["isConnected"] = False
        self.panel.after_write = drift
        with self.assertRaises(ValueError):
            apply.enable_de(self.state_dir, confirm=True, cdn_verified=True)
        self.assertEqual([path for _, path, _ in self.panel.writes], ["/internal-squads"])

    def test_lost_activation_patch_response_resumes_without_blind_repeat(self):
        for endpoint in ("/internal-squads", "/hosts"):
            for when in ("before", "after"):
                with self.subTest(endpoint=endpoint, when=when):
                    # Reset only synthetic new objects and manifest activation flag.
                    if not (self.state_dir / "manifest.json").exists():
                        self.healthy_host()
                    _row(self.panel.live["squads"], DE_SQUAD)["inbounds"] = [{"uuid": OLD_INBOUND}]
                    _row(self.panel.live["hosts"], NEW_HOST)["isDisabled"] = True
                    _row(self.panel.live["nodes"], NEW_NODE)["isConnected"] = True
                    state = json.loads((self.state_dir / "manifest.json").read_text())
                    state["activationRequested"] = False
                    (self.state_dir / "manifest.json").write_text(json.dumps(state))
                    self.panel.calls.clear()
                    self.panel.fail_once = ("PATCH", endpoint, when)
                    with self.assertRaises(TimeoutError):
                        apply.enable_de(self.state_dir, confirm=True, cdn_verified=True)
                    self.assertEqual(sum(path == endpoint for _, path, _ in self.panel.writes), 1)
                    self.panel.calls.clear()
                    apply.enable_de(self.state_dir, confirm=True, cdn_verified=True)
                    expected = (["/internal-squads", "/hosts"] if endpoint == "/internal-squads" and when == "before"
                                else ["/hosts"] if endpoint == "/internal-squads" or when == "before" else [])
                    self.assertEqual([path for _, path, _ in self.panel.writes], expected)
                    self.assertEqual([method for method, _, _ in self.panel.calls[:4]], ["GET"] * 4)

    def test_concurrent_de_removal_or_foreign_append_stops_activation(self):
        self.healthy_host()
        squad = _row(self.panel.live["squads"], DE_SQUAD)
        for value in ([], [{"uuid": OLD_INBOUND}, {"uuid": "foreign-inbound"}]):
            squad["inbounds"] = value
            self.panel.calls.clear()
            with self.assertRaises(ValueError):
                apply.enable_de(self.state_dir, confirm=True, cdn_verified=True)
            self.assert_readonly()

    def test_permissions_and_symlinks_fail_closed(self):
        self.core()
        manifest = self.state_dir / "manifest.json"
        manifest.chmod(0o644)
        self.panel.calls.clear()
        with self.assertRaises(ValueError):
            apply.audit(self.state_dir)
        self.assertEqual(self.panel.calls, [])
        manifest.chmod(0o600)
        copy_path = self.state_dir / "synthetic-manifest-copy.json"
        manifest.rename(copy_path)
        manifest.symlink_to(copy_path.name)
        with self.assertRaises(ValueError):
            apply.audit(self.state_dir)
        self.assertEqual(self.panel.calls, [])

    def test_abandoned_exclusive_save_file_blocks_before_api_write(self):
        self.core()
        plan.write(self.state_dir / "manifest.next.json", {"syntheticInterruptedWrite": True})
        self.panel.calls.clear()
        with self.assertRaises(FileExistsError):
            apply.audit(self.state_dir)
        self.assert_readonly()


if __name__ == "__main__":
    unittest.main()
