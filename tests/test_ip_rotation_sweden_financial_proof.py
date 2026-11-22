"""Offline proof of the one settled other-service pilot; never invoke RF_READ."""
import base64
from copy import deepcopy
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from spn_ip_failover.budget import AccountBudget, BudgetSettings
from spn_ip_failover.onecent import Settings, Service
from spn_ip_failover.provider import ProviderError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ops/ip-rotation-five"))
import enroll_netplan as enroll
spec = importlib.util.spec_from_file_location("sweden_financial_builder_test", ROOT / "ops/ip-rotation-five/build_sweden_agreement.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
epoch_spec = importlib.util.spec_from_file_location("sweden_financial_epoch_test", ROOT / "ops/ip-rotation-five/reconcile_sweden_epoch.py")
epoch = importlib.util.module_from_spec(epoch_spec)
epoch_spec.loader.exec_module(epoch)
SERVICE = "21e508f0-2ef2-47da-aee7-15dc8c66eb15"
SWEDEN = builder.SERVICE
REQUEST = "five-vps-pilot-20261116-001"
PRIMARY, IP = "8.8.8.8", "8.8.4.4"
CONTROLLERS = {"/var/lib/spn-ip-failover/state.json": None,
               "/var/lib/spn-ip-failover/rotation/state.json": None}


def record(value):
    raw = (json.dumps(value, sort_keys=True) + "\n").encode()
    return {"value": value, "sha256": hashlib.sha256(raw).hexdigest()}


def evidence(tmp_path):
    settings = Settings(tmp_path / "profile", tmp_path / "orders",
                        {row['service_id']: Service(row['service_name'], Decimal("3000.00"), Decimal("300.00"), frozenset())
                         for row in builder.SERVERS}, budget=BudgetSettings(tmp_path / "budget", Decimal("3000.00")))
    key = AccountBudget.key(SERVICE, REQUEST)
    body = b'{"fixture":"reviewed paid HTTP body"}'
    body_sha = hashlib.sha256(body).hexdigest()
    response = record({"format": 1, "request_key": REQUEST, "service_id": SERVICE, "expected_primary": PRIMARY,
                       "http_status": 200, "request": {"method": "POST", "path": "/api/services/" + SERVICE + "/ipv4/add",
                                                      "json": {"variantId": "reviewed-variant"}},
                       "idempotency_header_captured": False, "ownership_confirmed": False,
                       "requires_receipt_schema_review": True, "body_base64": base64.b64encode(body).decode(),
                       "body_bytes": len(body), "body_sha256": body_sha})
    intent = record({"format": 1, "request_key": REQUEST, "service_id": SERVICE, "expected_primary": PRIMARY,
                     "upfront_cap_rub": "100.00", "monthly_cap_rub": "300.00", "keep_current_primary": True})
    dispatch = record({"format": 1, "request_key": REQUEST, "service_id": SERVICE, "expected_primary": PRIMARY,
                       "upfront_rub": "14.21", "monthly_rub": "100.00", "variant_id": "reviewed-variant", "ownership_confirmed": False})
    proof = record({"version": 1, "deployment": builder.DEPLOYMENT, "service_id": SERVICE, "request_id": REQUEST,
                    "ip": IP, "response_sha256": response['sha256'], "intent_sha256": intent['sha256'],
                    "dispatch_sha256": dispatch['sha256']})
    entry = {"phase": "released", "before": {PRIMARY: True}, "request_id": REQUEST, "variant_id": "reviewed-variant",
             "budget": {"journal": str(settings.budget.journal.resolve()), "key": key},
             "upfront_rub": "14.21", "monthly_rub": "100.00", "ip": IP, "receipt_sha256": response['sha256'],
             "receipt": {"format": 1, "request_id": REQUEST, "service_id": SERVICE, "variant_id": "reviewed-variant",
                         "ip": IP, "response_sha256": body_sha}}
    orders = record({"version": 1, "servers": {SERVICE: {"orders": {REQUEST: entry}, "releases": {IP: "released"}}}})
    budget = record({"version": 1, "servers": {}, "budget": {"version": 1,
        "account": AccountBudget(settings.budget, settings.profile.resolve()).account, "timezone": "Europe/Saratov",
        "orders": {key: {"server_id": SERVICE, "request_id": REQUEST, "amount_rub": "14.21",
                         "days": ["2026-11-16"], "phase": "confirmed"}}}})
    return settings, orders, budget, deepcopy(CONTROLLERS), {"intent": intent, "dispatch": dispatch, "response": response, "proof": proof}


def summarize(parts):
    return builder.financial_summary(*parts)


def agreement(financial):
    from datetime import datetime, timezone
    return {"format": 1, "service_id": SWEDEN, "old_ip": builder.OLD, "new_ip": builder.NEW,
            "observed_at": datetime.now(timezone.utc).isoformat(), "user_confirmed_manual_change": True,
            "provider": {"service_id": SWEDEN, "name": epoch.SERVICE_NAME, "primary_ip": builder.NEW, "assigned_ipv4": [builder.NEW]},
            "remnawave": {"host_uuid": epoch.HOST, "node_uuid": epoch.NODE, "host_address": builder.NEW,
                          "node_address": builder.NEW, "host_node_uuids": [epoch.NODE], "node_connected": True,
                          "host_disabled": False, "node_disabled": False},
            "ssh": {"trusted_alias": epoch.SSH_ALIAS, "fingerprint": "SHA256:" + "A" * 43}, "financial": financial}


def check(financial):
    epoch.validate_agreement(agreement(financial), builder.OLD, builder.NEW, enroll)


def test_truthful_settled_pilot_keeps_global_count_fence_hashes_and_gross_charge(tmp_path):
    parts = evidence(tmp_path)
    untouched = deepcopy(parts[1:])
    financial = summarize(parts)
    assert parts[1:] == untouched
    assert financial["controller_orders"] == 1 and financial["pilot_fence_exists"] is True
    assert financial["sweden_attempts"] == financial["global_pending_orders"] == financial["pending_orders"] == 0
    assert financial["orders_journal_sha256"] == parts[1]["sha256"]
    assert financial["budget_journal_sha256"] == parts[2]["sha256"]
    row = financial["settled_other_orders"][0]
    assert row["amount_rub"] == "14.21" and row["budget_phase"] == "confirmed" and row["phase"] == "released"
    assert row["receipt_sha256"] == parts[4]["response"]["sha256"]
    assert row["response_body_sha256"] != row["receipt_sha256"]
    check(financial)


def test_absent_or_empty_journals_are_honest_zero_state_not_deleted(tmp_path):
    settings, _, _, controllers, _ = evidence(tmp_path)
    for orders in (None, record({"version": 1, "servers": {}})):
        financial = summarize((settings, orders, None, controllers, None))
        assert financial["controller_orders"] == 0 and financial["pilot_fence_exists"] is False
        assert financial["orders_journal_exists"] is (orders is not None)
        check(financial)


@pytest.mark.parametrize("mutation", [
    "submitting", "confirmed-not-released", "release-pending", "release-missing", "budget-submitting", "budget-no-purchase",
    "budget-amount", "budget-account", "body-hash-as-artifact", "artifact-hash-as-body", "proof-hash", "capture-body",
    "sweden-order", "extra-order", "extra-budget", "extra-release", "wrong-request", "legacy-only-receipt", "controller-tries",
    "controller-pending", "controller-retired", "pilot-fence-only", "missing-pilot", "proof-bool-version", "primary-bool-type",
])
def test_unknown_pending_mismatched_or_sweden_finances_never_pass(tmp_path, mutation):
    parts = list(evidence(tmp_path))
    _, orders, budget, controllers, pilot = parts
    entry = orders["value"]["servers"][SERVICE]["orders"][REQUEST]
    releases = orders["value"]["servers"][SERVICE]["releases"]
    payment = next(iter(budget["value"]["budget"]["orders"].values()))
    if mutation == "submitting": entry["phase"] = "submitting"
    if mutation == "confirmed-not-released": entry["phase"] = "confirmed"
    if mutation == "release-pending": releases[IP] = {"expected": {PRIMARY: True}}
    if mutation == "release-missing": releases.clear()
    if mutation == "budget-submitting": payment["phase"] = "submitting"
    if mutation == "budget-no-purchase": payment["phase"] = "no_purchase"
    if mutation == "budget-amount": payment["amount_rub"] = "13.00"
    if mutation == "budget-account": budget["value"]["budget"]["account"] = "a" * 64
    if mutation == "body-hash-as-artifact": entry["receipt_sha256"] = entry["receipt"]["response_sha256"]
    if mutation == "artifact-hash-as-body": entry["receipt"]["response_sha256"] = entry["receipt_sha256"]
    if mutation == "proof-hash": pilot["proof"]["value"]["response_sha256"] = "a" * 64
    if mutation == "capture-body": pilot["response"]["value"]["body_base64"] = base64.b64encode(b"different body").decode()
    if mutation == "sweden-order": orders["value"]["servers"][SWEDEN] = orders["value"]["servers"].pop(SERVICE)
    if mutation == "extra-order": orders["value"]["servers"][SERVICE]["orders"]["another"] = deepcopy(entry)
    if mutation == "extra-budget": budget["value"]["budget"]["orders"]["a" * 64] = deepcopy(payment)
    if mutation == "extra-release": releases["1.1.1.1"] = "released"
    if mutation == "wrong-request": entry["request_id"] = "another"
    if mutation == "legacy-only-receipt": del entry["receipt"]
    if mutation.startswith("controller-"):
        row = {"tries": 0, "pending": None, "retired": []}
        if mutation == "controller-tries": row["tries"] = 1
        if mutation == "controller-pending": row["pending"] = {"phase": "allocating"}
        if mutation == "controller-retired": row["retired"] = [{"id": IP, "ip": IP}]
        controllers["/var/lib/spn-ip-failover/state.json"] = record({"version": 1, "servers": {"sweden-gemini": row}})
    if mutation == "pilot-fence-only": parts[1] = parts[2] = None
    if mutation == "missing-pilot": parts[4] = None
    if mutation == "proof-bool-version": pilot["proof"]["value"]["version"] = True
    if mutation == "primary-bool-type": entry["before"][PRIMARY] = 1
    with pytest.raises((ValueError, KeyError, ProviderError)):
        summarize(parts)


@pytest.mark.parametrize("mutation", ["count", "fence", "pending", "sweden", "unreleased", "unconfirmed", "pilot-artifact", "journal-hash", "journal-missing", "controller-evidence"])
def test_extended_agreement_requires_matching_bounded_evidence(tmp_path, mutation):
    financial = summarize(evidence(tmp_path))
    row = financial["settled_other_orders"][0]
    if mutation == "count": financial["controller_orders"] = 0
    if mutation == "fence": financial["pilot_fence_exists"] = False
    if mutation == "pending": financial["global_pending_orders"] = 1
    if mutation == "sweden": row["service_id"] = SWEDEN
    if mutation == "unreleased": row["release_phase"] = "submitting"
    if mutation == "unconfirmed": row["budget_phase"] = "reserved"
    if mutation == "pilot-artifact": financial["pilot_evidence"]["response_sha256"] = "a" * 64
    if mutation == "journal-hash": financial["orders_journal_sha256"] = "unknown"
    if mutation == "journal-missing": financial["orders_journal_exists"] = False; financial["orders_journal_sha256"] = None
    if mutation == "controller-evidence": financial["controller_journals"].pop()
    with pytest.raises(ValueError):
        check(financial)


def test_remote_reader_is_read_only_and_checks_both_runtime_units_and_snapshot_stability():
    compile(builder.RF_READ, "<rf-read-only-proof>", "exec")
    assert "spn-ip-failover-five.service" in builder.RF_READ
    assert "Financial state changed during the read-only snapshot" in builder.RF_READ
    assert "O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK" in builder.RF_READ
    for forbidden in ("Journal(", "write_text(", "write_bytes(", "os.unlink(", "os.O_CREAT", "transition("):
        assert forbidden not in builder.RF_READ
