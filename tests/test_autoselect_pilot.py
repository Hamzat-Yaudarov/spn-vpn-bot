"""Offline tests for the Admin-only pilot's pure configuration helpers.

Import the operation script with its panel client replaced by a tripwire. These
tests use synthetic, keyless inputs and never load production state or call API.
"""

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest


def _reject_panel_call(*args, **kwargs):
    raise AssertionError("The autoselect unit tests must not contact the panel")


def _load_pilot():
    path = Path(__file__).resolve().parents[1] / "ops/autoselect-test-admin/manage.py"
    spec = importlib.util.spec_from_file_location("autoselect_test_admin", path)
    module = importlib.util.module_from_spec(spec)
    transport = ModuleType("scripts.prepare_direct_transports")
    transport.api = _reject_panel_call
    with patch.dict(sys.modules, {"scripts.prepare_direct_transports": transport}):
        spec.loader.exec_module(module)
    return module


pilot = _load_pilot()


EXPECTED_CANDIDATES = (
    "52edd62d-7e98-4b04-8cb4-67086a3660ea",  # NODE29 gRPC
    "cd19762c-685e-4d91-8124-2ddfa33ee59a",  # NODE29 WS
    "ac8404f0-0cf6-4709-9fad-be2ceb40dddd",  # CHISLO gRPC
    "b89c0f76-4497-4c06-b19f-9932433db3a4",  # CHISLO WS
    "b9087851-e6e5-45ac-9415-9c3b94d84125",  # TEKST gRPC
    "e82e5344-6d52-4c0e-98a6-3b9fa3696a2a",  # TEKST WS
    "5159d30e-0f9f-453d-ab34-f8bef6463b7a",  # PRESTIZH gRPC
    "5652e383-5030-444e-a72a-7b03b0fa3f20",  # PRESTIZH WS
)


@pytest.fixture
def source_config():
    return {
        "log": {"loglevel": "warning"},
        "dns": {"servers": ["https://dns.example.test/dns-query"]},
        "inbounds": [{"tag": "local-socks", "protocol": "socks", "port": 1080}],
        # Direct first deliberately exercises the conversion's default route.
        "outbounds": [
            {"tag": "direct", "protocol": "freedom", "settings": {"domainStrategy": "UseIP"}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "balancers": [
                {"tag": "Old_GRPC", "selector": ["grpc-"]},
                {"tag": "Old_WS", "selector": ["ws-"]},
            ],
            "rules": [
                {"type": "field", "domain": ["full:local.example.test"], "outboundTag": "direct"},
                {"type": "field", "ip": ["geoip:private"], "outboundTag": "direct"},
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
                {"type": "field", "domain": ["full:grpc.example.test"], "balancerTag": "Old_GRPC"},
                {"type": "field", "network": "tcp,udp", "balancerTag": "Old_WS"},
            ],
        },
        "observatory": {"subjectSelector": ["legacy-"], "probeInterval": "10s"},
        "remnawave": {"addVirtualHostAsOutbound": True, "injectHosts": []},
    }


def test_build_template_keeps_source_immutable_and_deeply_independent(source_config):
    before = copy.deepcopy(source_config)
    result = pilot.build_template(source_config)
    assert source_config == before
    assert result is not source_config
    result["dns"]["servers"].append("https://other.example.test/dns-query")
    result["routing"]["rules"][0]["domain"].append("full:changed.example.test")
    result["outbounds"][1]["settings"]["domainStrategy"] = "AsIs"
    assert source_config == before


def test_all_old_balanced_routes_use_one_prefix_balancer(source_config):
    result = pilot.build_template(source_config)
    balanced_rules = [r for r in result["routing"]["rules"] if "balancerTag" in r]
    assert len(balanced_rules) == 2
    assert {r["balancerTag"] for r in balanced_rules} == {pilot.BALANCER}
    assert result["routing"]["balancers"] == [{
        "tag": pilot.BALANCER,
        "selector": [pilot.PREFIX],
        "strategy": {"type": "leastPing"},
        "fallbackTag": "block",
    }]
    assert result["routing"]["domainStrategy"] == source_config["routing"]["domainStrategy"]


def test_intentional_direct_and_block_rules_and_order_are_preserved(source_config):
    result = pilot.build_template(source_config)
    expected_rules = copy.deepcopy(source_config["routing"]["rules"])
    for rule in expected_rules:
        if "balancerTag" in rule:
            rule["balancerTag"] = pilot.BALANCER
    assert result["routing"]["rules"] == expected_rules
    assert result["log"] == source_config["log"]
    assert result["dns"] == source_config["dns"]
    assert result["inbounds"] == source_config["inbounds"]


def test_fail_closed_fallback_and_unmatched_default_are_blackhole(source_config):
    result = pilot.build_template(source_config)
    assert result["outbounds"][0]["tag"] == "block"
    assert result["outbounds"][0]["protocol"] == "blackhole"
    outbounds = {o["tag"]: o for o in result["outbounds"]}
    fallback = result["routing"]["balancers"][0]["fallbackTag"]
    assert outbounds[fallback]["protocol"] == "blackhole"
    assert sorted(result["outbounds"], key=lambda o: o["tag"]) == sorted(
        source_config["outbounds"], key=lambda o: o["tag"]
    )


@pytest.mark.parametrize("bad_outbounds", [
    [{"tag": "direct", "protocol": "freedom"}],
    [{"tag": "block", "protocol": "freedom"}],
    [
        {"tag": "block", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
    ],
])
def test_missing_or_non_blackhole_block_outbound_is_rejected(source_config, bad_outbounds):
    source_config["outbounds"] = bad_outbounds
    before = copy.deepcopy(source_config)
    with pytest.raises((AssertionError, ValueError)):
        pilot.build_template(source_config)
    assert source_config == before


def test_source_proxy_outbounds_are_rejected_without_mutation(source_config):
    source_config["outbounds"].append({"tag": "old-proxy", "protocol": "vless"})
    before = copy.deepcopy(source_config)
    with pytest.raises((AssertionError, ValueError)):
        pilot.build_template(source_config)
    assert source_config == before


def test_injection_and_health_checks_cover_same_exclusive_proxy_prefix(source_config):
    result = pilot.build_template(source_config)
    injection = result["remnawave"]["injectHosts"]
    assert len(injection) == 1
    assert result["remnawave"]["addVirtualHostAsOutbound"] is False
    assert injection[0]["tagPrefix"] == pilot.PREFIX
    assert injection[0]["selectFrom"] == "ALL"
    assert injection[0]["selector"]["type"] == "uuids"
    assert result["routing"]["balancers"][0]["selector"] == [pilot.PREFIX]
    assert result["burstObservatory"]["subjectSelector"] == [pilot.PREFIX]
    assert "observatory" not in result
    assert all(not o["tag"].startswith(pilot.PREFIX) for o in result["outbounds"])


def test_candidate_allowlist_is_exactly_eight_direct_grpc_ws_hosts(source_config):
    result = pilot.build_template(source_config)
    selected = result["remnawave"]["injectHosts"][0]["selector"]["values"]
    assert tuple(pilot.CANDIDATES) == EXPECTED_CANDIDATES
    assert tuple(selected) == EXPECTED_CANDIDATES
    assert len(selected) == len(set(selected)) == 8
    # An exact UUID allowlist cannot pick additional HY2/CDN transports from ALL.
    assert pilot.SOURCE_HOST not in selected
    assert result["remnawave"]["injectHosts"][0]["selector"]["type"] != "all"


def test_returned_candidate_list_cannot_mutate_future_templates(source_config, monkeypatch):
    monkeypatch.setattr(pilot, "CANDIDATES", list(EXPECTED_CANDIDATES))
    first = pilot.build_template(source_config)
    first["remnawave"]["injectHosts"][0]["selector"]["values"].clear()
    assert tuple(pilot.CANDIDATES) == EXPECTED_CANDIDATES
    second = pilot.build_template(source_config)
    assert tuple(second["remnawave"]["injectHosts"][0]["selector"]["values"]) == EXPECTED_CANDIDATES


def test_validation_fixture_is_keyless_and_does_not_mutate_template(source_config):
    template = pilot.build_template(source_config)
    before = copy.deepcopy(template)
    fixture = pilot.validation_fixture(template)
    assert template == before
    assert "remnawave" not in fixture
    assert fixture["inbounds"] == []
    assert fixture["outbounds"][:len(template["outbounds"])] == template["outbounds"]
    placeholders = fixture["outbounds"][len(template["outbounds"]):]
    assert placeholders == [
        {"tag": pilot.PREFIX + str(i), "protocol": "freedom"}
        for i in range(len(EXPECTED_CANDIDATES))
    ]
    assert len({o["tag"] for o in fixture["outbounds"]}) == len(fixture["outbounds"])
    assert fixture["routing"] == template["routing"]
    assert fixture["burstObservatory"] == template["burstObservatory"]
    fixture["routing"]["rules"][0]["domain"].clear()
    fixture["outbounds"][0]["settings"]["response"] = {"type": "http"}
    assert template == before


@pytest.fixture
def validated_state(tmp_path, source_config):
    """Create only a synthetic, private local state with a valid verifier report."""
    template = pilot.build_template(source_config)
    pilot.save(tmp_path, "before.json", {"pilotTemplateHash": pilot.digest(template)})
    pilot.save(tmp_path, "validation-keyless.json", pilot.validation_fixture(template))
    report = {
        "fixtureSha256": hashlib.sha256((tmp_path / "validation-keyless.json").read_bytes()).hexdigest(),
        "ok": True,
        "compile": "passed",
        "cleanup": {"terminated": True},
        "initialSelection": "mock-fast",
        "failoverSelection": "mock-slow",
        "fallbackBlock": "verified: failed requests with no mock connection attempts",
        "realVpnEndToEnd": False,
        "subscriberConfigurationsRead": False,
    }
    pilot.save(tmp_path, "validation-result.json", report)
    return tmp_path, template, report


def test_validation_gate_accepts_matching_successful_report(validated_state):
    directory, template, _ = validated_state
    before = copy.deepcopy(template)
    pilot.require_validation(directory, template)
    assert template == before


def test_validation_gate_rejects_stale_fixture_even_with_matching_report_hash(validated_state):
    directory, template, report = validated_state
    stale = pilot.load(directory, "validation-keyless.json")
    stale["routing"]["rules"][0]["domain"].append("full:new.example.test")
    pilot.save(directory, "validation-keyless.json", stale)
    report["fixtureSha256"] = hashlib.sha256((directory / "validation-keyless.json").read_bytes()).hexdigest()
    pilot.save(directory, "validation-result.json", report)
    with pytest.raises(AssertionError, match="fixture is stale"):
        pilot.require_validation(directory, template)


@pytest.mark.parametrize("part", ["inbounds", "injection"])
def test_full_template_hash_rejects_mutations_removed_from_keyless_fixture(validated_state, part):
    directory, template, _ = validated_state
    original_fixture = pilot.validation_fixture(template)
    if part == "inbounds":
        template["inbounds"].append({"tag": "unexpected-listener", "protocol": "http", "port": 1090})
    else:
        template["remnawave"]["injectHosts"][0]["selector"]["values"].append("unexpected-hy2-or-cdn")
    # These changes are invisible to the fixture hash; the full prepared digest
    # must still reject them before any panel operation is authorized.
    assert pilot.validation_fixture(template) == original_fixture
    with pytest.raises(AssertionError, match="Prepared template changed"):
        pilot.require_validation(directory, template)


def test_validation_gate_requires_prepared_full_template_hash(validated_state):
    directory, template, _ = validated_state
    pilot.save(directory, "before.json", {})
    with pytest.raises(AssertionError, match="Prepared template changed"):
        pilot.require_validation(directory, template)


@pytest.mark.parametrize("part", ["candidates", "prefix", "selector", "scope", "virtual"])
def test_injection_guard_rejects_scope_changes_even_with_updated_prepared_hash(validated_state, part):
    directory, template, _ = validated_state
    remnawave = template["remnawave"]
    injection = remnawave["injectHosts"][0]
    if part == "candidates":
        injection["selector"]["values"].append("unexpected-hy2-or-cdn")
    elif part == "prefix":
        injection["tagPrefix"] = ""
    elif part == "selector":
        injection["selector"]["type"] = "all"
    elif part == "scope":
        injection["selectFrom"] = "VISIBLE"
    else:
        remnawave["addVirtualHostAsOutbound"] = True
    pilot.save(directory, "before.json", {"pilotTemplateHash": pilot.digest(template)})
    with pytest.raises(AssertionError, match="Unexpected pilot injection scope"):
        pilot.require_validation(directory, template)


def test_validation_gate_rejects_report_for_different_fixture_bytes(validated_state):
    directory, template, report = validated_state
    report["fixtureSha256"] = "0" * 64
    pilot.save(directory, "validation-result.json", report)
    with pytest.raises(AssertionError, match="report does not match fixture"):
        pilot.require_validation(directory, template)


def test_validation_gate_rejects_missing_fixture_hash(validated_state):
    directory, template, report = validated_state
    report.pop("fixtureSha256")
    pilot.save(directory, "validation-result.json", report)
    with pytest.raises(AssertionError, match="report does not match fixture"):
        pilot.require_validation(directory, template)


@pytest.mark.parametrize("field,bad_value", [
    ("ok", False),
    ("ok", 1),
    ("ok", "true"),
    ("compile", "failed"),
    ("compile", True),
    ("cleanup", {"terminated": False}),
    ("cleanup", {"terminated": 1}),
    ("cleanup", {"terminated": "true"}),
    ("cleanup", {}),
    ("initialSelection", "mock-slow"),
    ("failoverSelection", "mock-fast"),
    ("fallbackBlock", False),
    ("fallbackBlock", "unverified"),
    ("realVpnEndToEnd", True),
    ("subscriberConfigurationsRead", True),
])
def test_validation_gate_rejects_false_or_incorrect_success_flags(
    validated_state, field, bad_value
):
    directory, template, report = validated_state
    report[field] = bad_value
    pilot.save(directory, "validation-result.json", report)
    with pytest.raises(AssertionError):
        pilot.require_validation(directory, template)


@pytest.mark.parametrize("field", [
    "ok", "compile", "cleanup", "initialSelection", "failoverSelection",
    "fallbackBlock", "realVpnEndToEnd", "subscriberConfigurationsRead",
])
def test_validation_gate_rejects_missing_required_success_fields(validated_state, field):
    directory, template, report = validated_state
    report.pop(field)
    pilot.save(directory, "validation-result.json", report)
    with pytest.raises(AssertionError):
        pilot.require_validation(directory, template)


def test_apply_checks_verifier_success_before_any_panel_call(validated_state, monkeypatch):
    directory, template, report = validated_state
    pilot.save(directory, "created.json", {})
    pilot.save(directory, "template.json", template)
    report["ok"] = False
    pilot.save(directory, "validation-result.json", report)
    calls = []

    def unexpected_api(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Panel API reached before validation")

    monkeypatch.setattr(pilot, "api", unexpected_api)
    with pytest.raises(AssertionError, match="Validate Xray before panel writes"):
        pilot.apply(directory)
    assert calls == []


@pytest.fixture
def squads():
    return {
        "member-matching": {"inbounds": [{"uuid": "shared-inbound"}]},
        "member-other": {"inbounds": [{"uuid": "other-inbound"}]},
        "nonmember-matching": {"inbounds": [{"uuid": "shared-inbound"}]},
    }


def _host(**kwargs):
    return {"inbound": {"configProfileInboundUuid": "shared-inbound"}, **kwargs}


@pytest.mark.parametrize("restriction,members,expected", [
    (None, ["member-matching"], True),
    ({"mode": "EXCLUDE", "squads": []}, ["member-matching"], True),
    ({"mode": "EXCLUDE", "squads": ["member-matching"]}, ["member-matching"], False),
    ({"mode": "EXCLUDE", "squads": ["member-other"]}, ["member-other", "member-matching"], True),
    ({"mode": "ALLOW_ONLY", "squads": ["member-matching"]}, ["member-matching"], True),
    ({"mode": "ALLOW_ONLY", "squads": []}, ["member-matching"], False),
    ({"mode": "ALLOW_ONLY", "squads": ["nonmember-matching"]}, ["member-matching"], False),
    ({"mode": "ALLOW_ONLY", "squads": ["member-matching"]}, ["member-other"], False),
    (None, [], False),
    (None, ["unknown-squad"], False),
    (None, ["member-other"], False),
])
def test_visibility_applies_restriction_to_same_member_squad_with_inbound(
    squads, restriction, members, expected
):
    host = _host(internalSquads=restriction)
    before = copy.deepcopy((host, members, squads))
    assert pilot.visible(host, members, squads) is expected
    assert (host, members, squads) == before


@pytest.mark.parametrize("restriction", [
    {"mode": "ALLOW_ONLY", "squads": ["member-other"]},
    {"mode": "EXCLUDE", "squads": ["member-matching"]},
])
def test_different_squads_cannot_combine_permission_and_inbound(squads, restriction):
    # One squad has the required inbound; another passes the restriction. EXISTS
    # must be satisfied by the same squad, not by the union of the two squads.
    assert pilot.visible(
        _host(internalSquads=restriction), ["member-matching", "member-other"], squads
    ) is False


def test_excluded_squad_does_not_veto_another_permitted_matching_squad(squads):
    squads["second-matching"] = {"inbounds": [{"uuid": "shared-inbound"}]}
    host = _host(internalSquads={"mode": "EXCLUDE", "squads": ["member-matching"]})
    assert pilot.visible(host, ["member-matching", "second-matching"], squads) is True


def test_node_links_neither_grant_nor_prevent_subscription_visibility(squads):
    members = ["member-matching"]
    assert pilot.visible(_host(nodes=[]), members, squads) is True
    assert pilot.visible(_host(nodes=[{"uuid": "connected-node"}]), [], squads) is False


def test_missing_host_inbound_is_not_visible(squads):
    assert pilot.visible({"inbound": None}, ["member-matching"], squads) is False
    assert pilot.visible({}, ["member-matching"], squads) is False


@pytest.mark.parametrize("include_hidden", [True, False])
def test_disabled_host_is_never_visible(squads, include_hidden):
    assert pilot.visible(
        _host(isDisabled=True), ["member-matching"], squads, include_hidden=include_hidden
    ) is False


def test_hidden_host_visibility_matches_explicit_include_hidden_policy(squads):
    host = _host(isHidden=True)
    assert pilot.visible(host, ["member-matching"], squads) is True
    assert pilot.visible(host, ["member-matching"], squads, include_hidden=True) is True
    assert pilot.visible(host, ["member-matching"], squads, include_hidden=False) is False


def test_private_marker_requires_both_marker_membership_and_matching_inbound(squads):
    squads["private-marker"] = {"inbounds": [{"uuid": "shared-inbound"}]}
    host = _host(internalSquads={"mode": "ALLOW_ONLY", "squads": ["private-marker"]})
    assert pilot.visible(host, ["member-matching"], squads) is False
    assert pilot.visible(host, ["member-matching", "private-marker"], squads) is True
    squads["private-marker"]["inbounds"] = []
    assert pilot.visible(host, ["member-matching", "private-marker"], squads) is False
