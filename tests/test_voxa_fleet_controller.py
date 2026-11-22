import uuid

import pytest

from voxa_fleet.controller import Controller
from voxa_fleet.provider import ProviderError
from voxa_fleet.store import Store


PROJECT = "11111111-1111-4111-8111-111111111111"
PRODUCT = "22222222-2222-4222-8222-222222222222"
IMAGE = "33333333-3333-4333-8333-333333333333"


class FakeProvider:
    project_id = PROJECT

    def __init__(self, store):
        self.store, self.created, self.machines, self.cancelled = store, [], {}, []
        self.lose_create_response = False
        self.hide_orders = False
        self.cancel_unknown = False
        self.events = []

    def identity(self):
        return {"project": {"id": PROJECT, "slug": "fleet", "bound": True}}

    def balance(self):
        return {"balance": "100.00", "currency": "EUR"}

    def create_order(self, body, key):
        operations = [o for o in self.store.list_operations() if o["idempotency_key"] == key]
        assert len(operations) == 1
        op = operations[0]
        assert op["data"]["create_attempted"] is True
        assert op["data"]["order_body"] == body
        with self.store._connect() as db:
            assert db.execute("SELECT status FROM reservations WHERE operation_id=?", (op["id"],)).fetchone()[0] == "reserved"
        service, order_id = str(uuid.uuid4()), str(uuid.uuid4())
        order = {"id": order_id, "idempotency_key": key, "hostname": body["hostname"], "status": "paid", "services": [{"id": service, "status": "active"}]}
        self.created.append({"order": order, "services": [{"id": service}], "settled": True})
        self.machines[service] = {"id": service, "status": "active", "primary_ip": "192.0.2." + str(len(self.created) + 10), "instance": {"state": "running"}, "project": {"id": PROJECT, "slug": "fleet"}}
        self.events.append(("create", service))
        if self.lose_create_response:
            self.lose_create_response = False
            raise ProviderError("lost", outcome_unknown=True, code="TRANSPORT_ERROR")
        return self.created[-1]

    def order(self, order_id):
        return next(item for item in self.created if item["order"]["id"] == order_id)

    def orders(self, **filters):
        assert set(filters) == {"idempotencyKey"}
        return [] if self.hide_orders else [item["order"] for item in self.created if item["order"]["idempotency_key"] == filters["idempotencyKey"]]

    def vps(self, vps_id):
        return self.machines[vps_id]

    def cancel(self, service_id, reason, immediate=True):
        assert immediate is True
        assert service_id not in [s["current_vps_id"] for s in self.store.list_servers()]
        self.store.assert_deletable(service_id)
        self.cancelled.append(service_id)
        self.events.append(("cancel", service_id))
        self.machines[service_id]["status"] = "cancelled"
        if self.cancel_unknown:
            self.cancel_unknown = False
            raise ProviderError("lost cancellation", outcome_unknown=True, code="TRANSPORT_ERROR")
        return {"cancelled": True}


class FakeExecutor:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.is_ready = True
        self.controls_ok = True
        self.bad_candidate_count = 0
        self.crash_cutover = False
        self.cutover_ip = None

    def ready(self):
        return self.is_ready

    def round(self, rf=True, outside=True):
        return {"observed_at": self.clock[0], "controls_ok": self.controls_ok,
                "rf": [{"vpn_ok": rf}], "outside": [{"vpn_ok": outside}]}

    def prepare_order(self, server, op):
        self.calls.append(("prepare", op["id"]))
        return {"os_image_id": IMAGE, "ssh_public_key": "ssh-ed25519 PUBLIC_TEST_FIXTURE", "user_data": "#cloud-config\n# one-use public enrollment fixture"}

    def bootstrap(self, server, op, gen):
        self.calls.append(("bootstrap", gen["vps_id"]))
        return {"ok": True, "ids_preserved": True}

    def probe(self, server, gen):
        self.calls.append(("probe", gen["vps_id"]))
        if self.bad_candidate_count:
            self.bad_candidate_count -= 1
            return self.round(False, True)
        return self.round()

    def cutover(self, server, op, gen):
        self.calls.append(("cutover", gen["vps_id"]))
        self.cutover_ip = gen["ip"]
        if self.crash_cutover:
            self.crash_cutover = False
            raise SystemExit("simulated process loss after DNS effect")
        return {"ok": True, "ids_preserved": True, "dns_record_preserved": True}

    def recover(self, stage, server, op, gen):
        self.calls.append(("recover-" + stage, gen["vps_id"]))
        if stage == "cutover":
            assert self.cutover_ip == gen["ip"]
            return {"ok": True, "ids_preserved": True, "dns_record_preserved": True}
        return {"ok": True, "ids_preserved": True}

    def verify(self, server, op, gen):
        self.calls.append(("verify", gen["vps_id"]))
        return {"ok": True, "ids_preserved": True, "dns_converged": True, "round": self.round()}

    def restore(self, server, op, gen):
        self.calls.append(("restore", gen["vps_id"]))
        return {"ok": True, "ids_preserved": True}

    def observe(self, server):
        self.calls.append(("observe", server["id"]))
        return self.round(False, True)


@pytest.fixture
def fleet(tmp_path):
    clock = [1_800_000_000]
    store = Store(tmp_path / "state" / "fleet.db", clock=lambda: clock[0])
    store.update_settings({"financial_enabled": True, "terms_accepted": True, "daily_budget": "10", "max_hourly": "1", "grace_seconds": 0, "skip_ip_change_approved": True}, user_action=True)
    server = store.create_server({"name": "NL", "domain": "nl.example.test", "product_id": PRODUCT, "location": "nl", "currency": "EUR", "metadata": {"node_uuid": "original-node", "host_uuid": "original-host", "profile_uuid": "original-profile", "inbound_uuid": "original-inbound", "cloudflare_record_id": "original-record"}})
    provider, executor = FakeProvider(store), FakeExecutor(clock)

    def quote(server, payload):
        return {"product_id": PRODUCT, "location": "nl", "os_image_id": IMAGE, "currency": "EUR", "hourly_price": "0.0100", "initial_reserve": "0.1000", "availability_confirmed": True}

    return store, server, provider, executor, quote, clock


def make_controller(fleet, **kwargs):
    store, _, provider, executor, quote, _ = fleet
    return Controller(store, provider, executor, quote, **kwargs)


def queue(fleet, action="create", **payload):
    return fleet[0].enqueue_operation(action, fleet[1]["id"], {"approved": True, **payload})


def finish(controller, op, max_ticks=35):
    for _ in range(max_ticks):
        op = controller.tick(op["id"])
        if op["phase"] in {"complete", "failed", "manual-review"}:
            return op
    raise AssertionError("did not finish: " + op["phase"] + " " + str(op["data"]))


def test_actual_create_completes_with_honest_conservative_budget(fleet):
    store, server, provider, executor, _, _ = fleet
    op = finish(make_controller(fleet), queue(fleet))
    assert op["phase"] == "complete"
    assert op["data"]["accounting_kind"] == "conservative_upper_bound"
    assert len(provider.created) == 1
    assert provider.cancelled == []
    active = store.get_server(server["id"])
    assert active["current_ip"] == executor.cutover_ip
    assert active["metadata"]["node_uuid"] == "original-node"
    assert store.budget_status()["gross_daily"] == "0.1000"
    assert store.budget_status()["current_hourly"] == "0.0100"
    with store._connect() as db:
        reservation = dict(db.execute("SELECT * FROM reservations WHERE operation_id=?", (op["id"],)).fetchone())
    assert reservation["status"] == "accounted"
    assert reservation["actual_amount"] is None
    store.settle_budget(op["id"], "0.0100", currency="EUR")
    assert store.budget_status()["gross_daily"] == "0.1000"


def test_replace_keeps_uuid_identity_then_retires_only_owned_old(fleet):
    store, original, provider, executor, _, clock = fleet
    controller = make_controller(fleet)
    first = finish(controller, queue(fleet))
    old = store.get_server(original["id"])["current_vps_id"]
    before = store.get_server(original["id"])["metadata"].copy()
    store.update_settings({"grace_seconds": 120})
    op = queue(fleet, "replace")
    for _ in range(20):
        op = controller.tick(op["id"])
        if op["phase"] == "committed":
            break
    assert op["phase"] == "committed"
    current = store.get_server(original["id"])["current_vps_id"]
    assert current != old
    controller.tick(op["id"])
    assert provider.cancelled == []
    assert store.budget_status()["current_hourly"] == "0.0200"
    clock[0] += 121
    op = finish(controller, op)
    assert op["phase"] == "complete"
    assert provider.cancelled == [old]
    assert provider.events.index(("create", current)) < provider.events.index(("cancel", old))
    after = store.get_server(original["id"])["metadata"]
    assert {k: v for k, v in after.items() if k != "last_cutover_at"} == {k: v for k, v in before.items() if k != "last_cutover_at"}
    assert store.budget_status()["current_hourly"] == "0.0100"


def test_bad_candidate_restored_deleted_then_linked_child_reserves_again(fleet):
    store, server, provider, executor, _, _ = fleet
    executor.bad_candidate_count = 1
    controller = make_controller(fleet)
    parent = finish(controller, queue(fleet))
    assert parent["phase"] == "failed"
    child = store.get_operation(parent["data"]["child_operation_id"])
    assert child["payload"]["parent_operation_id"] == parent["id"]
    assert child["payload"]["attempt"] == 2
    rejected = parent["data"]["vps_id"]
    assert provider.cancelled == [rejected]
    assert ("restore", rejected) in executor.calls
    assert ("cutover", rejected) not in executor.calls
    assert len(provider.created) == 1
    assert store.budget_status()["pending_candidates"] == 0
    completed = finish(controller, child)
    assert completed["phase"] == "complete"
    assert len(provider.created) == 2
    assert store.budget_status()["gross_daily"] == "0.2000"


def test_unknown_create_reconciles_exact_key_after_restart_without_second_post(fleet):
    store, _, provider, executor, _, _ = fleet
    provider.lose_create_response = True
    controller = make_controller(fleet)
    op = controller.tick(queue(fleet)["id"])
    assert op["phase"] == "reserved"
    saved_body = op["data"]["order_body"]
    op = controller.tick(op["id"])
    assert op["phase"] == "outcome-unknown"
    assert store.budget_status()["pending_candidates"] == 1
    restarted = make_controller(fleet, worker_id="restarted")
    op = finish(restarted, op)
    assert op["phase"] == "complete"
    assert len(provider.created) == 1
    assert op["data"]["order_body"] == saved_body
    assert sum(name == "prepare" for name, _ in executor.calls) == 1


def test_unknown_absent_order_is_never_reposted_or_released(fleet):
    store, _, provider, _, _, _ = fleet
    provider.lose_create_response, provider.hide_orders = True, True
    controller = make_controller(fleet)
    op = controller.tick(queue(fleet)["id"])
    op = controller.tick(op["id"])
    for _ in range(4):
        op = controller.tick(op["id"])
    assert op["phase"] == "outcome-unknown"
    assert len(provider.created) == 1
    assert store.budget_status()["gross_daily"] == "0.1000"
    assert store.budget_status()["pending_candidates"] == 1


def test_no_money_when_executor_not_ready_or_controls_unknown(fleet):
    store, server, provider, executor, _, _ = fleet
    executor.is_ready = False
    op = make_controller(fleet).tick(queue(fleet)["id"])
    assert op["phase"] == "manual-review"
    assert provider.created == []
    assert store.budget_status()["gross_daily"] == "0"
    # A separate server avoids interfering with the unresolved first operation.
    other = store.create_server({"name": "Other", "domain": "other.example.test", "product_id": PRODUCT, "location": "nl"})
    executor.is_ready, executor.controls_ok = True, False
    op = store.enqueue_operation("replace", other["id"], {"approved": True})
    op = make_controller(fleet).tick(op["id"])
    assert op["phase"] == "manual-review"
    assert provider.created == []
    assert store.budget_status()["gross_daily"] == "0"


def test_cutover_crash_recovers_proof_instead_of_repeating_effect(fleet):
    store, _, provider, executor, _, _ = fleet
    executor.crash_cutover = True
    controller = make_controller(fleet)
    op = queue(fleet)
    with pytest.raises(SystemExit):
        for _ in range(20):
            op = controller.tick(op["id"])
    interrupted = store.get_operation(op["id"])
    assert interrupted["phase"] == "cutover"
    assert interrupted["data"]["cutover_started"] is True
    assert not interrupted["data"].get("cutover_done")
    completed = finish(make_controller(fleet), interrupted)
    assert completed["phase"] == "complete"
    assert sum(name == "cutover" for name, _ in executor.calls) == 1
    assert sum(name == "recover-cutover" for name, _ in executor.calls) == 1


def test_unknown_cancel_reconciles_terminal_state_without_second_cancel(fleet):
    _, _, provider, executor, _, _ = fleet
    executor.bad_candidate_count = 1
    provider.cancel_unknown = True
    parent = finish(make_controller(fleet), queue(fleet))
    assert parent["phase"] == "failed"
    assert len(provider.cancelled) == 1


def test_exact_cleanup_rejects_foreign_or_active_vps(fleet):
    store, server, provider, _, _, _ = fleet
    controller = make_controller(fleet)
    op = store.enqueue_operation("cleanup", server["id"], {"approved": True, "vps_id": "unrelated-manual-vps"})
    op = controller.tick(op["id"])
    assert op["phase"] == "manual-review"
    assert provider.cancelled == []


def test_automatic_observation_needs_multiple_good_control_rounds(fleet):
    store, server, provider, executor, _, clock = fleet
    store.update_server(server["id"], {"mode": "auto"})
    store.update_settings({"auto_enabled": True}, user_action=True)
    controller = make_controller(fleet)
    for _ in range(2):
        result = controller.observe_once()
        assert not result[0]["replacement_allowed"]
        clock[0] += 30
    result = controller.observe_once()
    assert result[0]["replacement_allowed"]
    assert result[0]["operation_id"]
    assert provider.created == []
    assert store.budget_status()["gross_daily"] == "0"


def test_uncertain_quote_or_currency_never_reserves_or_posts(fleet):
    store, _, provider, executor, quote, _ = fleet
    def uncertain(server, payload):
        return {**quote(server, payload), "fees_unknown": True}
    controller = Controller(store, provider, executor, uncertain)
    op = controller.tick(queue(fleet)["id"])
    assert op["phase"] == "manual-review"
    assert provider.created == []
    assert store.budget_status()["gross_daily"] == "0"


def test_owner_cleanup_resumes_manual_review_without_new_purchase(fleet):
    store, server, provider, executor, _, _ = fleet
    controller = make_controller(fleet)
    op = queue(fleet)
    for _ in range(20):
        op = controller.tick(op["id"])
        if op["phase"] == "bootstrap" and op["data"].get("bootstrap_done"):
            break
    executor.controls_ok = False
    op = controller.tick(op["id"])
    assert op["phase"] == "manual-review"
    target = op["data"]["vps_id"]
    resumed = store.enqueue_operation("cleanup", server["id"], {"approved": True, "vps_id": target})
    assert resumed["id"] == op["id"]
    assert resumed["phase"] == "cleanup-pending"
    completed = finish(controller, resumed)
    assert completed["phase"] == "failed"
    assert "child_operation_id" not in completed["data"]
    assert provider.cancelled == [target]
    assert len(provider.created) == 1
    assert ("restore", target) in executor.calls


def test_imported_previous_vps_remains_nonowned_and_billable_after_cutover(fleet):
    store, _, provider, _, _, _ = fleet
    imported = store.create_server({"name": "Imported", "domain": "import.example.test", "product_id": PRODUCT, "location": "nl", "current_vps_id": str(uuid.uuid4()), "current_ip": "192.0.2.99", "hourly_price": "0.02"})
    op = store.enqueue_operation("replace", imported["id"], {"approved": True})
    finished = finish(make_controller(fleet), op)
    assert finished["phase"] == "manual-review"
    assert store.get_server(imported["id"])["current_vps_id"] != imported["current_vps_id"]
    assert provider.cancelled == []
    assert store.budget_status()["current_hourly"] == "0.0300"


def test_attempt_ceiling_prevents_new_linked_purchase(fleet):
    store, _, provider, executor, _, _ = fleet
    store.update_settings({"max_attempts": 1})
    executor.bad_candidate_count = 1
    finished = finish(make_controller(fleet), queue(fleet))
    assert finished["phase"] == "failed"
    assert not finished["data"].get("child_operation_id")
    assert len(provider.created) == 1
    assert len(provider.cancelled) == 1


def test_executor_errors_never_leak_raw_output_into_journal(fleet):
    store, _, provider, executor, _, _ = fleet
    secret = "PRIVATE_SUBPROCESS_OUTPUT_DO_NOT_LOG"
    def fail(*args):
        raise RuntimeError(secret)
    executor.bootstrap = fail
    finished = finish(make_controller(fleet), queue(fleet))
    assert finished["phase"] == "manual-review"
    assert secret not in str(store.list_operations())
    assert secret not in str(store.list_events())
    assert len(provider.created) == 1


def registration(store):
    return {"original_state_path": str(store.path.parent / "installation" / "state.json"),
            "metadata": {"node_id": "44444444-4444-4444-8444-444444444444", "host_id": "55555555-5555-4555-8555-555555555555",
                         "profile_id": "66666666-6666-4666-8666-666666666666", "inbound_id": "77777777-7777-4777-8777-777777777777",
                         "cloudflare_record_id": "a" * 32, "installation_code": "NL-INITIAL-01"}}


def test_first_create_adopts_once_and_recovers_identical_registration(fleet):
    store, _, provider, executor, _, _ = fleet
    fresh = store.create_server({"name": "Fresh", "domain": "fresh.example.test", "product_id": PRODUCT, "location": "nl", "metadata": {"squads": ["original-squad"], "create_request_id": "original-request"}})
    op = store.enqueue_operation("create", fresh["id"], {"approved": True})
    evidence = registration(store)
    bootstrap, recover = executor.bootstrap, executor.recover
    executor.bootstrap = lambda *args: {**bootstrap(*args), "registration": evidence}
    executor.recover = lambda stage, *args: {**recover(stage, *args), **({"registration": evidence} if stage == "bootstrap" else {})}
    controller = make_controller(fleet)
    save = controller._save
    def crash_after_atomic_adoption(phase=None, **data):
        if data.get("bootstrap_done"):
            raise SystemExit("simulated loss after registration commit")
        return save(phase, **data)
    controller._save = crash_after_atomic_adoption
    with pytest.raises(SystemExit):
        for _ in range(20):
            op = controller.tick(op["id"])
    registered = store.get_server(fresh["id"])
    assert registered["original_state_path"] == evidence["original_state_path"]
    assert registered["metadata"]["squads"] == ["original-squad"]
    assert registered["metadata"]["create_request_id"] == "original-request"
    for key, value in evidence["metadata"].items():
        assert registered["metadata"][key] == value
    resumed = store.get_operation(op["id"])
    assert resumed["data"]["identity_snapshot"]["original_state_path"] == evidence["original_state_path"]
    completed = finish(make_controller(fleet), resumed)
    assert completed["phase"] == "complete"
    assert sum(name == "bootstrap" for name, _ in executor.calls) == 1
    assert sum(name == "recover-bootstrap" for name, _ in executor.calls) == 1
    assert sum(event["kind"] == "identity.registered" for event in store.list_events()) == 1
    assert len(provider.created) == 1


def test_replacement_cannot_adopt_new_logical_ids(fleet):
    store, server, _, executor, _, _ = fleet
    controller = make_controller(fleet)
    assert finish(controller, queue(fleet))["phase"] == "complete"
    original = store.get_server(server["id"])
    bootstrap = executor.bootstrap
    executor.bootstrap = lambda *args: {**bootstrap(*args), "registration": registration(store)}
    rejected = finish(controller, queue(fleet, "replace"))
    assert rejected["phase"] == "manual-review"
    current = store.get_server(server["id"])
    assert current["current_vps_id"] == original["current_vps_id"]
    assert current["metadata"] == original["metadata"]
    assert current["original_state_path"] == original["original_state_path"]
    assert not rejected["data"].get("cutover_started")
