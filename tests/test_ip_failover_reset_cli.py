"""Explicit local purchase reauthorization never calls hosting, DNS or panel."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from spn_ip_failover import __main__ as cli
from spn_ip_failover.controller import Conflict
from spn_ip_failover.state import Journal


class ResetLimitTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.state = Path(directory) / "state"
        self.server = SimpleNamespace(key="pilot", provider="1cent", fingerprint="pin")
        self.config = SimpleNamespace(servers=(self.server,), providers={})
        self.row = {"fingerprint": "pin", "tries": 10, "exhausted": True,
                    "regional_by_host": {"host": 3}, "healthy": 2,
                    "current": {"id": "current", "ip": "8.8.8.8"},
                    "pending": None, "retired": [], "hosts": {"host": {"hidden": False}},
                    "rejected_history": ["8.8.4.4"]}
        self.write(self.row)

    def write(self, row):
        with Journal(self.state) as journal:
            journal.data["servers"]["pilot"] = copy.deepcopy(row)
            journal.save()

    def arguments(self, **overrides):
        return SimpleNamespace(command="reset-limit", server="pilot", apply=True,
                               state=str(self.state), **overrides)

    def test_reset_requires_explicit_apply(self):
        args = self.arguments()
        args.apply = False
        with self.assertRaisesRegex(ValueError, "--apply"):
            cli.execute(args, self.config, {})
        self.assertEqual(Journal(self.state).snapshot()["servers"]["pilot"], self.row)

    def test_reset_keeps_current_ip_and_cleanup_metadata_and_never_uses_external_clients(self):
        with patch.object(cli, "Remnawave", side_effect=AssertionError("Network forbidden")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.execute(self.arguments(), self.config, {}), 0)
        row = Journal(self.state).snapshot()["servers"]["pilot"]
        self.assertEqual(row["tries"], 0)
        self.assertFalse(row["exhausted"])
        self.assertEqual(row["current"], self.row["current"])
        self.assertEqual(row["hosts"], self.row["hosts"])
        self.assertEqual(row["retired"], self.row["retired"])
        self.assertEqual(row["rejected_history"], self.row["rejected_history"])
        self.assertEqual(row["regional_by_host"], {})
        self.assertEqual(row["healthy"], 0)
        self.assertEqual(row["limit_resets"][0]["previous_tries"], 10)
        self.assertEqual(json.loads(output.getvalue())["event"], "purchase_limit_manually_reset")

    def test_unknown_pending_order_or_retirement_prevents_reauthorization(self):
        for changes in ({"pending": {"phase": "allocating", "request_id": "unknown"}},
                        {"retired": [{"id": "pending-retirement", "ip": "1.1.1.1"}]}):
            before = {**copy.deepcopy(self.row), **changes}
            self.write(before)
            with self.subTest(changes=changes), self.assertRaises(Conflict):
                cli.execute(self.arguments(), self.config, {})
            self.assertEqual(Journal(self.state).snapshot()["servers"]["pilot"], before)

    def test_reset_only_declared_managed_server_with_existing_journal(self):
        args = self.arguments()
        args.server = "outside-scope"
        with self.assertRaises(ValueError):
            cli.execute(args, self.config, {})
        self.server.provider = None
        with self.assertRaises(ValueError):
            cli.execute(self.arguments(), self.config, {})
        self.server.provider = "1cent"
        with Journal(self.state) as journal:
            journal.data["servers"] = {}
            journal.save()
        with self.assertRaisesRegex(ValueError, "нет журнала"):
            cli.execute(self.arguments(), self.config, {})

    def test_cli_parses_explicit_scoped_reset_without_authentication_or_network(self):
        with patch.object(cli, "load_config", return_value=self.config), \
                patch.object(cli, "Remnawave", side_effect=AssertionError("Network forbidden")), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["--config", "unused-private.json", "--state", str(self.state),
                             "reset-limit", "--server", "pilot", "--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(Journal(self.state).snapshot()["servers"]["pilot"]["tries"], 0)


if __name__ == "__main__":
    unittest.main()
