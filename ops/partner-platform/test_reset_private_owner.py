"""Offline invariants for the fixed private-owner reset; never load config/network."""

from contextlib import redirect_stdout
from copy import deepcopy
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


SOURCE = Path(__file__).with_name("reset_private_owner.py")
SPEC = importlib.util.spec_from_file_location("_offline_reset_private_owner", SOURCE)
reset = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reset)

PID = "8175b112-bc10-4468-a5d0-a0898d620db3"
OID = "f378dc14-c7df-4a34-8be6-8201a163f9a3"
BOT = 200002
WHEN = "2026-10-22T12:00:00+00:00"


def tables():
    """One paid owner pilot, but no paid/issued customer subscriptions."""
    data = {name: [] for name in reset.TABLES}
    data["wl_projects"] = [{"id": PID, "owner_tg_id": reset.OWNER, "status": "active",
                            "bot_id": BOT, "encrypted_token": "offline-not-a-real-cipher"}]
    data["wl_entry_orders"] = [{"id": OID, "project_id": PID, "owner_tg_id": reset.OWNER,
                                "status": "launched", "price_kopecks": 1000, "currency": "RUB",
                                "paid_at": WHEN, "launched_at": WHEN,
                                "provider_payment_id": "offline-saved-owner-payment", "bot_id": BOT}]
    data["wl_launch_jobs"] = [{"project_id": PID, "order_id": OID, "bot_id": BOT,
                               "status": "complete", "completed_at": WHEN}]
    data["wl_partner_outbox"] = [
        {"project_id": PID, "order_id": OID, "kind": kind, "delivery_state": state}
        for kind, state in (("entry_paid", "delivered"), ("entry_launched", "pending"))
    ]
    data["wl_managed_bot_requests"] = [{"project_id": PID, "owner_tg_id": reset.OWNER,
                                        "bot_id": BOT, "status": "bound"}]
    data["wl_manager_identities"] = [{"tg_id": reset.OWNER}]
    for name in ("wl_customers", "wl_campaign_attributions", "wl_marketing_preferences"):
        data[name] = [{"project_id": PID, "tg_id": reset.OWNER}]
    data["wl_subscriptions"] = [{"project_id": PID, "tg_id": reset.OWNER,
                                 "remote_user_id": None, "subscription_url": None,
                                 "expires_at": None, "desired_expires_at": None,
                                 "last_activation_sequence": 0}]
    data["wl_orders"] = [
        {"project_id": PID, "tg_id": reset.OWNER, "status": status, "paid_at": None,
         "provider_payment_id": None, "confirmation_url": None, "invoice_requested_at": None}
        for status in ("created", "canceled")
    ]
    for name in ("wl_project_create_requests", "wl_project_audit", "wl_management_requests",
                 "wl_campaigns", "wl_brand_assets", "wl_brand_asset_audit"):
        data[name] = [{"project_id": PID, "owner_tg_id": reset.OWNER}]
    return data


def edge(child, parent, child_schema="public", parent_schema="public"):
    return {"child": child, "parent": parent,
            "child_schema": child_schema, "parent_schema": parent_schema}


class PlanTests(unittest.TestCase):
    def output(self, args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = reset.main(args)
        return code, json.loads(out.getvalue())

    def test_default_plan_has_no_config_file_helper_command_or_network_io(self):
        with patch.object(reset, "reset", AsyncMock(side_effect=AssertionError("must not run"))) as run, \
             patch.object(reset, "load_helper", side_effect=AssertionError("helper I/O")), \
             patch.object(reset.Path, "lstat", side_effect=AssertionError("filesystem I/O")), \
             patch.object(reset.os, "open", side_effect=AssertionError("filesystem I/O")), \
             patch.object(reset.subprocess, "run", side_effect=AssertionError("command I/O")), \
             patch.object(reset.urllib.request, "build_opener", side_effect=AssertionError("network I/O")):
            code, value = self.output([])
        self.assertEqual(code, 0)
        self.assertEqual(value, {"plan_only": True, "target": reset.TARGET, "owner": reset.OWNER,
                                 "database_read": False, "changes": False})
        run.assert_not_awaited()

    def test_both_fixed_confirmations_without_explicit_reset_still_only_plan(self):
        with patch.object(reset, "reset", AsyncMock()) as run:
            code, value = self.output(["--confirm-target", reset.TARGET, "--confirm-owner", str(reset.OWNER)])
        self.assertEqual(code, 0)
        self.assertTrue(value["plan_only"])
        run.assert_not_awaited()

    def test_only_explicit_reset_with_exact_fixed_target_and_owner_calls_async_reset(self):
        with patch.object(reset, "reset", AsyncMock(return_value={"reset": True, "offline": True})) as run:
            code, value = self.output(["--reset-owner-test", "--confirm-target", reset.TARGET,
                                       "--confirm-owner", str(reset.OWNER)])
        self.assertEqual((code, value), (0, {"reset": True, "offline": True}))
        run.assert_awaited_once_with()

    def test_missing_foreign_or_unknown_arguments_fail_redacted_without_reset(self):
        for args in (
            ["--reset-owner-test"],
            ["--reset-owner-test", "--confirm-target", reset.TARGET],
            ["--reset-owner-test", "--confirm-owner", str(reset.OWNER)],
            ["--reset-owner-test", "--confirm-target", "192.0.2.100", "--confirm-owner", str(reset.OWNER)],
            ["--reset-owner-test", "--confirm-target", reset.TARGET, "--confirm-owner", str(reset.OWNER + 1)],
            ["--config", "/private-secret-config"], ["--token", "private-secret"],
        ):
            with self.subTest(args=args), patch.object(reset, "reset", AsyncMock()) as run:
                code, value = self.output(args)
                self.assertEqual(code, 2)
                self.assertEqual(value, {"reset": False, "error": "private_owner_reset_failed",
                                         "retry": "inspect_backup_and_units_first"})
                run.assert_not_awaited()


class ValidationTests(unittest.TestCase):
    def test_exact_32_tables_allow_one_owner_paid_10_entry_completed_launch_unpaid_orders(self):
        data = tables()
        self.assertEqual(len(data), 32)
        self.assertEqual(set(data), reset.TABLES)
        self.assertTrue(all(not data[name] for name in reset.EMPTY_REQUIRED))
        self.assertIs(reset.validate_tables(data), data["wl_projects"][0])
        self.assertEqual({row["status"] for row in data["wl_orders"]}, {"created", "canceled"})

    def test_table_set_and_row_container_types_are_exact(self):
        cases = [None, [], {**tables(), "wl_unreviewed": []}]
        missing = tables(); missing.pop("wl_finance_audit"); cases.append(missing)
        for replacement in ({}, ["not-a-row"]):
            wrong = tables(); wrong["wl_customers"] = replacement; cases.append(wrong)
        for data in cases:
            with self.subTest(data_type=type(data).__name__), self.assertRaises(reset.ResetError):
                reset.validate_tables(data)

    def test_exactly_one_project_entry_and_launch_are_required(self):
        for name in ("wl_projects", "wl_entry_orders", "wl_launch_jobs"):
            for count in (0, 2):
                data = tables(); data[name] *= count
                with self.subTest(name=name, count=count), self.assertRaises(reset.ResetError):
                    reset.validate_tables(data)

    def test_foreign_owner_actor_and_project_rows_are_rejected(self):
        cases = [("wl_projects", "owner_tg_id", reset.OWNER + 1),
                 ("wl_entry_orders", "owner_tg_id", reset.OWNER + 1),
                 ("wl_manager_identities", "tg_id", reset.OWNER + 1)]
        cases += [(name, "tg_id", reset.OWNER + 1) for name in
                  ("wl_customers", "wl_subscriptions", "wl_orders", "wl_campaign_attributions", "wl_marketing_preferences")]
        cases += [(name, "project_id", "00000000-0000-4000-8000-000000000001")
                  for name, rows in tables().items() if rows and name not in {"wl_projects", "wl_manager_identities"}]
        for name, key, value in cases:
            data = tables(); data[name][0][key] = value
            with self.subTest(name=name, key=key), self.assertRaises(reset.ResetError):
                reset.validate_tables(data)

    def test_every_money_grant_activation_and_external_side_effect_table_must_be_empty(self):
        for name in sorted(reset.EMPTY_REQUIRED):
            data = tables(); data[name] = [{"project_id": PID, "tg_id": reset.OWNER, "amount": 1}]
            with self.subTest(name=name), self.assertRaises(reset.ResetError):
                reset.validate_tables(data)

    def test_entry_requires_owner_10_rubles_paid_launched_and_matching_bot(self):
        for key, value in (("status", "paid_pending_launch"), ("price_kopecks", 999000),
                           ("currency", "USDT"), ("paid_at", None), ("launched_at", None),
                           ("provider_payment_id", None), ("bot_id", BOT + 1)):
            data = tables(); data["wl_entry_orders"][0][key] = value
            with self.subTest(key=key), self.assertRaises(reset.ResetError):
                reset.validate_tables(data)
        for key, value in (("status", "draft"), ("bot_id", True), ("bot_id", 0), ("encrypted_token", None)):
            data = tables(); data["wl_projects"][0][key] = value
            with self.subTest(project_key=key), self.assertRaises(reset.ResetError):
                reset.validate_tables(data)

    def test_launch_must_be_complete_and_match_the_paid_entry(self):
        for key, value in (("status", "pending"), ("order_id", "different-order"),
                           ("bot_id", BOT + 1), ("completed_at", None)):
            data = tables(); data["wl_launch_jobs"][0][key] = value
            with self.subTest(key=key), self.assertRaises(reset.ResetError):
                reset.validate_tables(data)

    def test_paid_customer_or_any_customer_invoice_evidence_is_rejected(self):
        for key, value in (("status", "active"), ("paid_at", WHEN),
                           ("provider_payment_id", "customer-payment"),
                           ("confirmation_url", "https://pay.example.test/customer"),
                           ("invoice_requested_at", WHEN)):
            data = tables(); data["wl_orders"][0][key] = value
            with self.subTest(key=key), self.assertRaises(reset.ResetError):
                reset.validate_tables(data)

    def test_remote_vpn_subscription_or_desired_activation_is_rejected(self):
        for key, value in (("remote_user_id", "remote-id"), ("subscription_url", "https://vpn.example.test/s"),
                           ("expires_at", WHEN), ("desired_expires_at", WHEN), ("last_activation_sequence", 1)):
            data = tables(); data["wl_subscriptions"][0][key] = value
            with self.subTest(key=key), self.assertRaises(reset.ResetError):
                reset.validate_tables(data)

    def test_outbox_and_managed_binding_cannot_target_other_orders_or_bots(self):
        for name, key, value in (("wl_partner_outbox", "order_id", "foreign-order"),
                                 ("wl_partner_outbox", "kind", "customer_paid"),
                                 ("wl_partner_outbox", "delivery_state", "processing"),
                                 ("wl_managed_bot_requests", "bot_id", BOT + 1)):
            data = tables(); data[name][0][key] = value
            with self.subTest(name=name, key=key), self.assertRaises(reset.ResetError):
                reset.validate_tables(data)


class SnapshotAndOrderTests(unittest.TestCase):
    def test_row_and_dictionary_order_are_irrelevant_but_duplicate_count_is_exact(self):
        before = tables()
        after = {name: [dict(reversed(list(row.items()))) for row in reversed(rows)]
                 for name, rows in reversed(list(before.items()))}
        self.assertTrue(reset.same_rows(before, after))
        after["wl_orders"].append(deepcopy(after["wl_orders"][0]))
        self.assertFalse(reset.same_rows(before, after))
        self.assertFalse(reset.same_rows(before, {**before, "unexpected": []}))

    def test_precise_json_decimals_and_nested_types_are_not_float_coerced(self):
        value = json.loads('{"n":0.123456789012345678901234567890,"nested":{"b":true,"a":[1,null]}}', parse_float=Decimal)
        before = {"table": [value]}
        same = json.loads('{"nested":{"a":[1,null],"b":true},"n":0.123456789012345678901234567890}', parse_float=Decimal)
        self.assertTrue(reset.same_rows(before, {"table": [same]}))
        for mutation in (Decimal("0.123456789012345678901234567891"), float(value["n"])):
            changed = deepcopy(value); changed["n"] = mutation
            self.assertFalse(reset.same_rows(before, {"table": [changed]}))
        changed = deepcopy(value); changed["nested"]["b"] = 1
        self.assertFalse(reset.same_rows(before, {"table": [changed]}))
        changed = deepcopy(value); changed["nested"]["a"].reverse()
        self.assertFalse(reset.same_rows(before, {"table": [changed]}))

    def test_foreign_keys_delete_children_before_parents_with_deterministic_order(self):
        edges = [edge("wl_orders", "wl_projects"), edge("wl_subscriptions", "wl_customers"),
                 edge("wl_customers", "wl_projects"), edge("wl_activation_jobs", "wl_orders"),
                 edge("wl_launch_jobs", "wl_entry_orders"), edge("wl_entry_orders", "wl_projects"),
                 edge("wl_customers", "wl_customers")]
        order = reset.deletion_order(edges)
        self.assertEqual(len(order), 32)
        self.assertEqual(set(order), reset.TABLES)
        self.assertEqual(order, reset.deletion_order(list(reversed(edges))))
        for item in edges:
            if item["child"] != item["parent"]:
                self.assertLess(order.index(item["child"]), order.index(item["parent"]))
        self.assertEqual(reset.deletion_order([]), sorted(reset.TABLES))

    def test_cycles_and_external_or_nonpublic_foreign_keys_are_rejected(self):
        cases = [
            [edge("wl_orders", "wl_projects"), edge("wl_projects", "wl_orders")],
            [edge("wl_orders", "wl_customers"), edge("wl_customers", "wl_projects"), edge("wl_projects", "wl_orders")],
            [edge("unreviewed_child", "wl_projects")], [edge("wl_orders", "unreviewed_parent")],
            [edge("wl_orders", "wl_projects", "private", "public")],
            [edge("wl_orders", "wl_projects", "public", "auth")],
        ]
        for edges in cases:
            with self.subTest(edges=edges), self.assertRaises(reset.ResetError):
                reset.deletion_order(edges)


class Context:
    def __init__(self, value): self.value = value; self.error = None
    async def __aenter__(self): return self.value
    async def __aexit__(self, kind, error, traceback): self.error = error


class FakeConnection:
    def __init__(self, data, *, edges=(), bad_delete=False, remaining=0):
        self.data, self.edges, self.bad_delete, self.remaining = data, list(edges), bad_delete, remaining
        self.calls, self.transactions = [], []
    def transaction(self, **kwargs):
        self.calls.append(("transaction", kwargs))
        context = Context(self); self.transactions.append(context); return context
    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if sql.startswith("DELETE"):
            name = sql.split('"')[1]
            return "DELETE " + str(len(self.data[name]) + int(self.bad_delete))
        return "OK"
    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if "FROM pg_constraint" in sql: return self.edges
        return [{"name": name, "count": self.remaining} for name in sorted(reset.TABLES)]


class ClearRecordsTests(unittest.IsolatedAsyncioTestCase):
    async def run_clear(self, data, *, snapshot=None, **kwargs):
        conn = FakeConnection(data, **kwargs)
        pool = Mock(); pool.acquire.return_value = Context(conn)
        with patch.object(reset, "current_rows", AsyncMock(return_value=deepcopy(data) if snapshot is None else snapshot)):
            await reset.clear_records(pool, data)
        return conn

    async def test_transaction_locks_and_only_scoped_nonempty_deletes_never_money_tables(self):
        data = tables()
        conn = await self.run_clear(data, edges=[edge("wl_entry_orders", "wl_projects"), edge("wl_launch_jobs", "wl_entry_orders")])
        self.assertEqual(conn.calls[0], ("transaction", {"isolation": "serializable"}))
        deletes = [(call[1], call[2]) for call in conn.calls
                   if call[0] == "execute" and call[1].startswith("DELETE")]
        names = [sql.split('"')[1] for sql, _ in deletes]
        self.assertEqual(set(names), {name for name, rows in data.items() if rows})
        self.assertTrue(set(names).isdisjoint(reset.EMPTY_REQUIRED))
        self.assertLess(names.index("wl_launch_jobs"), names.index("wl_entry_orders"))
        self.assertLess(names.index("wl_entry_orders"), names.index("wl_projects"))
        for sql, args in deletes:
            if '"wl_projects"' in sql:
                self.assertIn("WHERE id=$1 AND owner_tg_id=$2", sql)
                self.assertEqual((str(args[0]), args[1]), (PID, reset.OWNER))
            elif '"wl_manager_identities"' in sql:
                self.assertTrue(sql.endswith("WHERE tg_id=$1")); self.assertEqual(args, (reset.OWNER,))
            else:
                self.assertTrue(sql.endswith("WHERE project_id=$1")); self.assertEqual(str(args[0]), PID)
        self.assertIsNone(conn.transactions[0].error)

    async def test_changed_locked_snapshot_rejected_before_any_delete(self):
        data = tables(); changed = deepcopy(data); changed["wl_project_audit"].append({"project_id": PID})
        conn = FakeConnection(data); pool = Mock(); pool.acquire.return_value = Context(conn)
        with patch.object(reset, "current_rows", AsyncMock(return_value=changed)), self.assertRaises(reset.ResetError):
            await reset.clear_records(pool, data)
        self.assertFalse(any(call[0] == "execute" and call[1].startswith("DELETE") for call in conn.calls))
        self.assertIsInstance(conn.transactions[0].error, reset.ResetError)

    async def test_external_fk_rejected_before_delete_and_mismatched_counts_rollback(self):
        for kwargs in ({"edges": [edge("external_records", "wl_projects")]}, {"bad_delete": True}, {"remaining": 1}):
            data = tables(); conn = FakeConnection(data, **kwargs); pool = Mock(); pool.acquire.return_value = Context(conn)
            with patch.object(reset, "current_rows", AsyncMock(return_value=deepcopy(data))), self.assertRaises(reset.ResetError):
                await reset.clear_records(pool, data)
            self.assertIsInstance(conn.transactions[0].error, reset.ResetError)
            if "edges" in kwargs:
                self.assertFalse(any(call[0] == "execute" and call[1].startswith("DELETE") for call in conn.calls))


class CommitOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def outcome(self, current=None, *, read_error=None, pool_error=None):
        original = tables()
        settings = object()  # Never construct runtime settings or load configuration.
        conn = FakeConnection(original)
        pool = Mock(); pool.acquire.return_value = Context(conn)
        opener = Mock(side_effect=pool_error) if pool_error else Mock(return_value=Context(pool))
        module = types.ModuleType("scripts.partner_runtime")
        module.open_pool = opener
        reader = AsyncMock(side_effect=read_error) if read_error else AsyncMock(return_value=current)
        with patch.dict(sys.modules, {"scripts.partner_runtime": module}), \
             patch.object(reset, "current_rows", reader):
            result = await reset.commit_outcome(settings, original)
        opener.assert_called_once_with(settings)
        return result, conn, pool, reader

    async def test_all_32_tables_empty_means_committed_from_new_readonly_pool(self):
        result, conn, pool, reader = await self.outcome({name: [] for name in reset.TABLES})
        self.assertEqual(result, "committed")
        pool.acquire.assert_called_once_with()
        reader.assert_awaited_once_with(conn)
        self.assertEqual(conn.calls, [("transaction", {"isolation": "repeatable_read", "readonly": True})])
        self.assertIsNone(conn.transactions[0].error)

    async def test_exact_original_snapshot_with_reordered_rows_means_rolled_back(self):
        original = tables()
        reordered = {name: [dict(reversed(list(row.items()))) for row in reversed(rows)]
                     for name, rows in reversed(list(original.items()))}
        result, conn, pool, reader = await self.outcome(reordered)
        self.assertEqual(result, "rolled_back")
        reader.assert_awaited_once_with(conn)
        self.assertEqual(conn.calls, [("transaction", {"isolation": "repeatable_read", "readonly": True})])

    async def test_mixed_or_changed_nonempty_snapshot_is_unknown_not_rollback(self):
        mixed = tables(); mixed["wl_orders"].pop()
        only_project = {name: [] for name in reset.TABLES}
        only_project["wl_projects"] = tables()["wl_projects"]
        for current in (mixed, only_project):
            result, conn, pool, reader = await self.outcome(current)
            self.assertEqual(result, "unknown")
            self.assertFalse(any(call[0] == "execute" for call in conn.calls))
        changed = tables(); changed["wl_projects"][0]["encrypted_token"] = "changed-after-uncertain-commit"
        self.assertEqual((await self.outcome(changed))[0], "unknown")

    async def test_new_pool_or_snapshot_network_failure_is_unknown_and_no_write(self):
        result, conn, pool, reader = await self.outcome(pool_error=RuntimeError("private-network-secret"))
        self.assertEqual(result, "unknown")
        self.assertEqual(conn.calls, [])
        pool.acquire.assert_not_called()
        reader.assert_not_awaited()
        result, conn, pool, reader = await self.outcome(read_error=RuntimeError("private-read-secret"))
        self.assertEqual(result, "unknown")
        self.assertEqual(conn.calls, [("transaction", {"isolation": "repeatable_read", "readonly": True})])
        self.assertIsInstance(conn.transactions[0].error, RuntimeError)
        self.assertNotIn("private", result)


if __name__ == "__main__":
    unittest.main()
