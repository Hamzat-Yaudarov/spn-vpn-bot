import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spn_ip_failover.provider import Allocation, CommandProvider, ProviderError, ProviderNotSubmitted


class CommandProviderTests(unittest.TestCase):
    def test_end_to_end_command_protocol(self):
        helper = """
import json
import sys

request = json.load(sys.stdin)
with open(sys.argv[1], "a", encoding="utf-8") as transcript:
    print(json.dumps(request), file=transcript)
if request["operation"] == "allocate":
    print(json.dumps({"id": "new", "ip": "8.8.4.4"}))
else:
    print(json.dumps({"ok": True}))
"""
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "requests.jsonl"
            command = [sys.executable, "-c", helper, str(transcript)]
            provider = CommandProvider({operation: command for operation in CommandProvider.operations})
            old = Allocation("old", "8.8.8.8")
            provider.check("srv-1")
            new = provider.allocate("srv-1", "rotation-1")
            self.assertEqual(new, Allocation("new", "8.8.4.4"))
            provider.prepare("srv-1", new)
            provider.activate("srv-1", old, new)
            provider.release("srv-1", old)
            requests = [json.loads(line) for line in transcript.read_text().splitlines()]

        self.assertEqual(requests, [
            {"operation": "check", "server_id": "srv-1"},
            {"operation": "allocate", "server_id": "srv-1", "request_id": "rotation-1"},
            {"operation": "prepare", "server_id": "srv-1", "id": "new", "ip": "8.8.4.4"},
            {"operation": "activate", "server_id": "srv-1", "old": old.as_payload(), "new": new.as_payload()},
            {"operation": "release", "server_id": "srv-1", "id": "old", "ip": "8.8.8.8"},
        ])

    def test_unconfirmed_allocation_stays_an_error(self):
        provider = CommandProvider({
            operation: [sys.executable] for operation in CommandProvider.operations
        })
        bad_replies = [
            b'{"id":"allocation","id":"second","ip":"8.8.4.4"}',
            b'{"id":"allocation","ip":"127.0.0.1"}',
            b'{"id":"allocation","ip":"224.0.0.1"}',
            b'{"id":"allocation","ip":"8.8.4.4","token":"secret"}',
        ]
        for reply in bad_replies:
            with self.subTest(reply=reply), patch(
                "spn_ip_failover.provider.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=reply),
            ):
                with self.assertRaisesRegex(ProviderError, "outcome unconfirmed") as failure:
                    provider.allocate("srv-1", "rotation-1")
                self.assertNotIn("secret", str(failure.exception))

    def test_only_exact_definitive_budget_denial_is_not_submitted(self):
        provider = CommandProvider({operation: [sys.executable] for operation in CommandProvider.operations})
        responses = [
            ({"error": "not_submitted", "reason": "budget_limit"}, ProviderNotSubmitted),
            ({"error": "not_submitted", "reason": "unknown"}, ProviderError),
            ({"error": "not_submitted", "reason": "budget_limit", "secret": "hidden"}, ProviderError),
            ({"error": "maybe_submitted", "reason": "budget_limit"}, ProviderError),
        ]
        for response, expected in responses:
            with self.subTest(response=response), patch.object(provider, "_call", return_value=response):
                with self.assertRaises(expected) as failure:
                    provider.allocate("srv-1", "rotation-1")
                self.assertEqual(type(failure.exception), expected)
                self.assertNotIn("hidden", str(failure.exception))

    def test_failed_process_cannot_claim_definitive_not_submitted(self):
        provider = CommandProvider({operation: [sys.executable] for operation in CommandProvider.operations})
        with patch("spn_ip_failover.provider.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 1, stdout=b'{"error":"not_submitted","reason":"budget_limit"}',
        )):
            with self.assertRaises(ProviderError) as failure:
                provider.allocate("srv-1", "rotation-1")
            self.assertNotIsInstance(failure.exception, ProviderNotSubmitted)

    def test_refuses_ambiguous_success_and_unsafe_commands(self):
        commands = {operation: [sys.executable] for operation in CommandProvider.operations}
        with self.assertRaisesRegex(ValueError, "absolute path"):
            CommandProvider({**commands, "release": ["python3"]})
        provider = CommandProvider(commands)
        with patch(
            "spn_ip_failover.provider.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=b'{"ok":1}'),
        ):
            with self.assertRaisesRegex(ProviderError, "did not confirm success"):
                provider.release("srv-1", Allocation("old", "8.8.8.8"))

    def test_inventory_requires_unique_allocations(self):
        commands = {operation: [sys.executable] for operation in CommandProvider.operations}
        with self.assertRaisesRegex(ProviderError, "inventory command is missing"):
            CommandProvider(commands).inventory("srv-1")
        provider = CommandProvider({**commands, "inventory": [sys.executable]})
        item = {"id": "current", "ip": "8.8.8.8"}
        responses = [
            {"allocations": [item]},
            {"allocations": [item, {"id": "duplicate", "ip": item["ip"]}]},
        ]
        with patch.object(provider, "_call", side_effect=responses):
            self.assertEqual(provider.inventory("srv-1"), (Allocation("current", "8.8.8.8"),))
            with self.assertRaisesRegex(ProviderError, "unique IPv4 allocations"):
                provider.inventory("srv-1")
