import multiprocessing
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest

from voxa_fleet.store import BudgetError, ConflictError, Store, StoreError


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "private" / "fleet.db", clock=lambda: 1_800_000_000)


def server(store, **kwargs):
    return store.create_server({"name": "NL", "domain": "nl.example.test", **kwargs})


def authorize(store, **kwargs):
    return store.update_settings({"daily_budget": "1.00", "max_hourly": "0.10", "terms_accepted": True, "financial_enabled": True, **kwargs}, user_action=True)


def purchase(store, sid=None, **kwargs):
    return store.enqueue_operation("create" if sid is None else "replace", sid, **kwargs)


def _claim_in_process(path, queue, worker):
    result = Store(path, clock=lambda: 1_800_000_000).claim_operation(worker)
    queue.put(result["id"] if result else None)


def _reserve_in_process(path, queue, opid):
    try:
        Store(path, clock=lambda: 1_800_000_000).reserve_budget(opid, "0.60", "0.01", balance="10.00")
        queue.put(True)
    except BudgetError:
        queue.put(False)


def test_private_sqlite_durability_and_restart(store):
    assert os.stat(store.path.parent).st_mode & 0o777 == 0o700
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with store._connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    original = server(store)
    assert Store(store.path).get_server(original["id"])["domain"] == "nl.example.test"


def test_reject_symlink_hardlink_and_shared_directory(tmp_path):
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    target = state / "real.db"
    target.touch(mode=0o600)
    link = state / "linked.db"
    link.symlink_to(target)
    with pytest.raises((StoreError, OSError)):
        Store(link)
    link.unlink()
    os.link(target, link)
    with pytest.raises(StoreError):
        Store(target)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(StoreError):
        Store(shared / "db")


def test_defaults_no_money_or_implicit_terms(store):
    assert store.get_settings()["financial_enabled"] is False
    assert store.get_settings()["daily_budget"] == "0"
    with pytest.raises(StoreError):
        store.update_settings({"financial_enabled": True}, user_action=True)
    with pytest.raises(StoreError):
        authorize_without_user = {"daily_budget": "1", "max_hourly": "1", "terms_accepted": True, "financial_enabled": True}
        store.update_settings(authorize_without_user)
    assert not store.get_settings()["terms_accepted"]
    with pytest.raises(StoreError):
        store.update_settings({"max_attempts": 11})
    with pytest.raises(StoreError):
        store.update_settings({"daily_budget": 0.1})
    with pytest.raises(BudgetError):
        store.reserve_budget(purchase(store)["id"], "0.1", "0.01", balance="10")


def test_operation_dedup_and_payload_identity(store):
    sid = server(store)["id"]
    first = store.enqueue_operation("replace", sid, {"reason": "rf"}, idempotency_key="same")
    assert store.enqueue_operation("replace", sid, {"reason": "rf"}, idempotency_key="same")["id"] == first["id"]
    assert store.enqueue_operation("replace", sid, {"reason": "rf"})["id"] == first["id"]
    with pytest.raises(ConflictError):
        store.enqueue_operation("replace", sid, {"reason": "other"}, idempotency_key="same")
    with pytest.raises(ConflictError):
        store.enqueue_operation("check", sid)
    malicious = server(store, name="x'); DROP TABLE servers; --")
    assert store.get_server(malicious["id"])["name"] == malicious["name"]


def test_cas_lease_and_state_machine(store):
    op = purchase(store)
    claimed = store.claim_operation("worker")
    assert store.claim_operation("other") is None
    with pytest.raises(ConflictError):
        store.update_operation(op["id"], expected_version=op["version"], worker_id="worker", phase="reserved")
    with pytest.raises(ConflictError):
        store.update_operation(op["id"], expected_version=claimed["version"], worker_id="worker", phase="committed")
    updated = store.update_operation(op["id"], expected_version=claimed["version"], worker_id="worker", phase="reserved")
    assert updated["version"] == claimed["version"] + 1
    assert store.claim_operation("other", now=1_800_000_061)["id"] == op["id"]
    with pytest.raises(ConflictError):
        store.update_operation(op["id"], expected_version=updated["version"], worker_id="worker", phase="ordered")
    assert store.acquire_lease("worker", "one")
    assert not store.acquire_lease("worker", "two")
    assert not store.release_lease("worker", "two")
    assert store.acquire_lease("worker", "two", now=1_800_000_061)


def test_cross_process_claim_is_exclusive(store):
    purchase(store)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    workers = [ctx.Process(target=_claim_in_process, args=(store.path, queue, name)) for name in ("one", "two")]
    for worker in workers:
        worker.start()
    outcomes = [queue.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert sum(outcome is not None for outcome in outcomes) == 1


def test_budget_exact_atomic_daily_and_currency(store):
    authorize(store, max_candidates=3)
    one, two = purchase(store), purchase(store)
    store.reserve_budget(one["id"], Decimal("0.60"), "0.01", balance="10")
    assert store.reserve_budget(one["id"], Decimal("0.60"), "0.01", balance="10")["amount"] == "0.60"
    with pytest.raises(BudgetError):
        store.reserve_budget(two["id"], "0.41", "0.01", balance="10")
    with pytest.raises(BudgetError):
        store.reserve_budget(two["id"], "0.10", "0.01", currency="USD", balance="10")
    store.settle_budget(one["id"], "0.60")
    assert store.budget_status()["gross_daily"] == "0.60"
    with pytest.raises(BudgetError):
        store.release_budget(one["id"], confirmed_no_charge=True)
    with pytest.raises(ConflictError):
        store.settle_budget(one["id"], "0.01")


def test_money_aggregation_ignores_ambient_decimal_rounding(store):
    authorize(store, max_candidates=3)
    with localcontext() as context:
        context.prec = 2
        for _ in range(2):
            store.reserve_budget(purchase(store)["id"], "0.123456789012345678901234", "0.00001", balance="10")
        assert store.budget_status()["gross_daily"] == "0.246913578024691357802468"


def test_cross_process_budget_comparison_is_atomic(store):
    authorize(store, max_candidates=3)
    operations = [purchase(store)["id"] for _ in range(2)]
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    workers = [ctx.Process(target=_reserve_in_process, args=(store.path, queue, opid)) for opid in operations]
    for worker in workers:
        worker.start()
    outcomes = [queue.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert sorted(outcomes) == [False, True]


def test_saratov_day_and_unknown_outcome_retains_limits(store):
    authorize(store)
    op = purchase(store)
    before_midnight = datetime(2026, 11, 16, 19, 59, 59, tzinfo=timezone.utc)
    midnight = datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc)
    store.reserve_budget(op["id"], "0.1", "0.01", balance="10", now=before_midnight)
    store.release_budget(op["id"])
    assert store.budget_status(before_midnight)["day"] == "2026-11-16"
    assert store.budget_status(before_midnight)["gross_daily"] == "0.1"
    assert store.budget_status(midnight)["day"] == "2026-11-17"
    assert store.budget_status(midnight)["gross_daily"] == "0"
    assert store.budget_status(midnight)["pending_candidates"] == 1
    with pytest.raises(BudgetError):
        store.reserve_budget(purchase(store)["id"], "0.1", "0.01", balance="10", now=midnight)


def test_hourly_cap_counts_current_and_candidates(store):
    authorize(store, max_candidates=2, max_hourly="0.015")
    server(store, current_vps_id="imported-current", hourly_price="0.01")
    with pytest.raises(BudgetError):
        store.reserve_budget(purchase(store)["id"], "0.1", "0.01", balance="10")


def test_unknown_current_cost_and_server_currency_block_spending(store):
    authorize(store)
    unknown = server(store, current_vps_id="imported", hourly_price="0")
    with pytest.raises(BudgetError):
        store.reserve_budget(purchase(store, unknown["id"])["id"], "0.1", "0.01", balance="10")
    store.update_server(unknown["id"], {"hourly_price": "0.01"})
    other = server(store, currency="USD")
    with pytest.raises(BudgetError):
        store.reserve_budget(purchase(store, other["id"])["id"], "0.1", "0.01", balance="10")


def test_generation_ownership_active_protection_and_cleanup(store):
    authorize(store)
    logical = server(store, metadata={"node_uuid": "preserved"})
    op = purchase(store, logical["id"])
    store.reserve_budget(op["id"], "0.1", "0.01", balance="10")
    with pytest.raises(StoreError):
        store.assert_deletable("unrelated-manual-vps")
    store.record_generation(op["id"], "exact-order", "owned-vps", ip="192.0.2.10", hourly_price="0.01")
    with pytest.raises(StoreError):
        store.assert_deletable("owned-vps", "other-operation")
    store.update_generation("owned-vps", {"status": "vpn-tested"})
    store.commit_generation("owned-vps")
    with pytest.raises(StoreError):
        store.assert_deletable("owned-vps")
    assert store.get_server(logical["id"])["metadata"]["node_uuid"] == "preserved"
    assert store.budget_status()["current_hourly"] == "0.01"


def test_known_deleted_candidate_no_longer_counts_hourly(store):
    authorize(store)
    op = purchase(store)
    store.reserve_budget(op["id"], "0.1", "0.01", balance="10")
    store.record_generation(op["id"], "order", "candidate", hourly_price="0.01")
    store.settle_budget(op["id"], "0.1")
    store.update_generation("candidate", {"status": "deleted"})
    assert store.budget_status()["current_hourly"] == "0"
    assert store.budget_status()["pending_candidates"] == 0
    assert store.budget_status()["gross_daily"] == "0.1"


def test_pending_finance_cannot_be_reported_complete(store):
    authorize(store)
    op = purchase(store)
    store.reserve_budget(op["id"], "0.1", "0.01", balance="10")
    claimed = store.claim_operation("worker")
    with pytest.raises(ConflictError):
        store.update_operation(op["id"], expected_version=claimed["version"], worker_id="worker", phase="complete")
    store.record_probe(server(store)["id"], {"controls_ok": False})
    assert any(event["kind"] == "budget.reserved" for event in store.list_events())


def test_settled_but_unresolved_generation_is_not_complete(store):
    authorize(store)
    op = purchase(store)
    store.reserve_budget(op["id"], "0.1", "0.01", balance="10")
    store.record_generation(op["id"], "order", "pending")
    store.settle_budget(op["id"], "0.1")
    claimed = store.claim_operation("worker")
    with pytest.raises(ConflictError):
        store.update_operation(op["id"], expected_version=claimed["version"], worker_id="worker", phase="complete")
