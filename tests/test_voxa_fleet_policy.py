from voxa_fleet.policy import classify_health, evaluate_replacement


NOW = 1_800_000_000


def round_(rf=(True,), outside=(True,), **kwargs):
    return {"observed_at": NOW, "controls_ok": True,
            "rf": [{"vpn_ok": value} for value in rf],
            "outside": [{"vpn_ok": value} for value in outside], **kwargs}


def test_health_ssh_never_changes_vpn_verdict():
    assert classify_health(round_(ssh_ok=False), now=NOW)["verdict"] == "healthy"
    assert classify_health(round_((False,), (True,), ssh_ok=True), now=NOW)["verdict"] == "ru_unreachable"
    assert classify_health(round_((False,), (False,), ssh_ok=True), now=NOW)["verdict"] == "service_down"
    assert classify_health(round_((True, False)), now=NOW)["verdict"] == "partial"


def test_empty_stale_control_failure_or_missing_real_vpn_are_unknown():
    rounds = [[], {}, round_(rf=()), round_(outside=()), round_(controls_ok=False),
              round_(observed_at=NOW - 100), round_(rf=[True], outside=[True], observed_at=NOW + 100),
              {"observed_at": NOW, "controls_ok": True, "rf": [{"ssh_ok": True}], "outside": [{"vpn_ok": True}]}]
    for probes in rounds:
        result = classify_health(probes, now=NOW)
        assert result["verdict"] == "unknown"
        assert not result["eligible"]
    independent_stale = round_()
    independent_stale["outside"][0]["observed_at"] = NOW - 100
    assert classify_health(independent_stale, now=NOW)["verdict"] == "unknown"
    independent_bad_control = round_()
    independent_bad_control["outside"][0]["controls_ok"] = False
    assert classify_health(independent_bad_control, now=NOW)["verdict"] == "unknown"


def config():
    return ({"enabled": True, "mode": "auto", "created_at": NOW - 300},
            {"auto_enabled": True, "financial_enabled": True, "skip_ip_change_approved": True,
             "consecutive_failures": 3, "probe_interval": 30, "probe_max_age": 90, "grace_seconds": 120})


def failures():
    return [round_((False,), observed_at=NOW - ago) for ago in (0, 30, 60)]


def test_requires_consecutive_fresh_independent_rounds():
    server, settings = config()
    assert evaluate_replacement(server, settings, failures(), now=NOW)["allowed"]
    assert not evaluate_replacement(server, settings, failures()[:2], now=NOW)["allowed"]
    duplicate_rounds = [failures()[0]] * 3
    assert not evaluate_replacement(server, settings, duplicate_rounds, now=NOW)["allowed"]
    with_gap = [round_((False,), observed_at=NOW - ago) for ago in (0, 120, 150)]
    assert not evaluate_replacement(server, settings, with_gap, now=NOW)["allowed"]
    recovered = failures()
    recovered[1] = round_(observed_at=NOW - 30)
    assert not evaluate_replacement(server, settings, recovered, now=NOW)["allowed"]


def test_mode_authorization_grace_and_explicit_manual_ip_step():
    server, settings = config()
    for mode in ("observe", "approval"):
        assert not evaluate_replacement({**server, "mode": mode}, settings, failures(), now=NOW)["allowed"]
    for field in ("auto_enabled", "financial_enabled", "skip_ip_change_approved"):
        assert not evaluate_replacement(server, {**settings, field: False}, failures(), now=NOW)["allowed"]
    assert not evaluate_replacement({**server, "metadata": {"last_cutover_at": NOW - 30}}, settings, failures(), now=NOW)["allowed"]
    assert not evaluate_replacement(server, settings, [round_(ssh_ok=False)] * 3, now=NOW)["allowed"]
