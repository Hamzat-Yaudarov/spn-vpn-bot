"""Offline parser/renderer tests; no host, DNS, or route changes."""

import copy
import io
import json
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import ip_move
from ip_move import MigrationError, build_route, render_ifupdown


ESTONIA = """# Provider-managed settings
auto lo
iface lo inet loopback

auto ens3
iface ens3 inet static
    address   31.59.218.221
    netmask   255.255.255.255
    gateway   10.0.0.1

auto ens3:0
iface ens3:0 inet static
    address   95.85.242.156
    netmask   255.255.255.255
    post-up /usr/sbin/ip -4 route change default via 10.0.0.1 dev ens3 onlink src 95.85.242.156

auto ens3:1
iface ens3:1 inet static
    address   64.188.82.148
    netmask   255.255.255.255
"""


class RouteArgumentsTests(unittest.TestCase):
    def test_onlink_route_changes_only_preferred_source(self):
        row = {
            "dst": "default",
            "gateway": "10.0.0.1",
            "dev": "ens3",
            "prefsrc": "95.85.242.156",
            "flags": ["onlink"],
        }
        original = copy.deepcopy(row)
        args = build_route(row, "ens3", "31.59.218.234")
        self.assertEqual(args[0], "default")
        self.assertEqual(args[args.index("via") + 1], row["gateway"])
        self.assertEqual(args[args.index("dev") + 1], row["dev"])
        self.assertEqual(args[args.index("src") + 1], "31.59.218.234")
        self.assertEqual(args.count("src"), 1)
        self.assertIn("onlink", args)
        self.assertNotIn(row["prefsrc"], args)
        self.assertEqual(row, original)

    def test_ordinary_dhcp_route_preserves_metric_and_protocol(self):
        row = {
            "dst": "default",
            "gateway": "172.0.0.1",
            "dev": "eth0",
            "protocol": "dhcp",
            "metric": 100,
            "flags": [],
        }
        args = build_route(row, "eth0", None)
        self.assertEqual(args[0], "default")
        self.assertEqual(args[args.index("via") + 1], "172.0.0.1")
        self.assertEqual(args[args.index("dev") + 1], "eth0")
        self.assertEqual(args[args.index("proto") + 1], "dhcp")
        self.assertEqual(args[args.index("metric") + 1], "100")
        self.assertNotIn("src", args)
        self.assertNotIn("onlink", args)

    def test_refuses_another_interface_or_unrecognized_route(self):
        base = {"dst": "default", "dev": "ens3", "gateway": "10.0.0.1", "flags": []}
        with self.assertRaises(MigrationError):
            build_route(base, "eth0", "31.59.218.234")
        for field, value in (
            ("nexthops", [{"gateway": "10.0.0.2", "dev": "ens3", "weight": 1}]),
            ("encap", {"type": "seg6"}),
        ):
            with self.subTest(field=field), self.assertRaises(MigrationError):
                build_route({**base, field: value}, "ens3", "31.59.218.234")


class IfupdownTests(unittest.TestCase):
    def test_estonia_changes_exactly_two_lines_and_keeps_other_addresses(self):
        expected = ESTONIA.replace("95.85.242.156", "31.59.218.234")
        rendered, alias = render_ifupdown(ESTONIA, "ens3", "10.0.0.1", "31.59.218.234", 32)
        self.assertEqual(alias, "ens3:0")
        self.assertEqual(rendered, expected)

    def test_cidr_alias_keeps_the_original_prefix(self):
        original = (
            "auto eth0:5\niface eth0:5 inet static\n"
            "    address 95.85.242.156/24\n"
            "    post-up /usr/sbin/ip -4 route change default via 10.0.0.1 dev eth0 onlink src 95.85.242.156\n"
        )
        rendered, alias = render_ifupdown(original, "eth0", "10.0.0.1", "95.85.242.157", 24)
        self.assertEqual(alias, "eth0:5")
        self.assertEqual(rendered, original.replace("95.85.242.156", "95.85.242.157"))

    def test_refuses_prefix_mismatch_and_unrecognized_or_ambiguous_hook(self):
        with self.assertRaises(MigrationError):
            render_ifupdown(ESTONIA, "ens3", "10.0.0.1", "31.59.218.234", 24)

        hook = next(line for line in ESTONIA.splitlines(keepends=True) if "post-up" in line)
        for changed in (
            ESTONIA.replace(hook, ""),
            ESTONIA.replace(hook, hook * 2),
            ESTONIA.replace(hook, hook.replace("src 95.85.242.156", "src 64.188.82.148")),
        ):
            with self.subTest(changed=changed), self.assertRaises(MigrationError):
                render_ifupdown(changed, "ens3", "10.0.0.1", "31.59.218.234", 32)


class MigrationTests(unittest.TestCase):
    old = "95.85.242.156"
    new = "31.59.218.234"
    original = {"dst": "default", "dev": "eth0", "gateway": "172.0.0.1",
                "protocol": "dhcp", "metric": 100, "flags": []}

    @contextmanager
    def mocked_host(self, *, root=True):
        state = dict(self.original)

        def ip_json(*args):
            if args == ("address", "show", "scope", "global"):
                return [{"ifname": "eth0", "flags": ["UP"], "addr_info": [
                    {"family": "inet", "local": self.new, "prefixlen": 32},
                ]}]
            if args == ("rule", "show"):
                return [{"priority": priority, "src": "all", "table": table}
                        for priority, table in ((0, "local"), (32766, "main"), (32767, "default"))]
            if args == ("route", "show", "table", "main", "default"):
                return [dict(state)]
            if args == ("route", "get", "1.1.1.1"):
                return [{"dev": state["dev"], "gateway": state["gateway"],
                         "prefsrc": state.get("prefsrc", self.old)}]
            self.fail(f"Unexpected ip query: {args}")

        def route_command(*args):
            self.assertIn(args[:4], (("ip", "-4", "route", "change"), ("ip", "-4", "route", "replace")))
            if "src" in args:
                state["prefsrc"] = args[args.index("src") + 1]
            else:
                state.pop("prefsrc", None)
            return ""

        with TemporaryDirectory() as path, ExitStack() as stack:
            stack.enter_context(patch.object(ip_move.sys, "platform", "linux"))
            stack.enter_context(patch.object(ip_move.os, "geteuid", return_value=0 if root else 1000))
            stack.enter_context(patch.object(ip_move, "ip_json", side_effect=ip_json))
            commands = stack.enter_context(patch.object(ip_move, "command", side_effect=route_command))
            https = stack.enter_context(patch.object(ip_move, "check_https"))
            backup = stack.enter_context(patch.object(ip_move.tempfile, "mkdtemp", return_value=path))
            stdout = stack.enter_context(redirect_stdout(io.StringIO()))
            stderr = stack.enter_context(redirect_stderr(io.StringIO()))
            yield state, commands, https, backup, Path(path), stdout, stderr

    def test_default_invocation_is_an_offline_plan(self):
        with self.mocked_host(root=False) as (state, commands, https, backup, path, out, _):
            ip_move.migrate(self.new)
            commands.assert_not_called()
            https.assert_not_called()
            backup.assert_not_called()
            self.assertEqual(state, self.original)
            self.assertFalse(list(path.iterdir()))
            self.assertIn("Режим просмотра", out.getvalue())

    def test_apply_changes_source_and_retains_original_route_attributes(self):
        with self.mocked_host() as (state, commands, https, _, path, out, _err):
            ip_move.migrate(self.new, apply=True)
            commands.assert_called_once_with("ip", "-4", "route", "change", "default", "via", "172.0.0.1",
                                             "dev", "eth0", "proto", "dhcp", "metric", "100", "src", self.new)
            self.assertEqual(state, {**self.original, "prefsrc": self.new})
            self.assertEqual(json.loads((path / "route.json").read_text()), self.original)
            self.assertEqual([entry.args for entry in https.call_args_list], [(self.new,), (self.new,)])
            self.assertEqual([entry.kwargs for entry in https.call_args_list], [{"bind": True}, {}])
            self.assertIn(f"HTTPS 204 через {self.new}", out.getvalue())

    def test_https_failure_restores_exact_prior_route(self):
        def check(_new, *, bind=False):
            if not bind:
                raise MigrationError("Проверка HTTPS не прошла")

        with self.mocked_host() as (state, commands, https, _, path, _out, err):
            https.side_effect = check
            with self.assertRaisesRegex(MigrationError, "Проверка HTTPS"):
                ip_move.migrate(self.new, apply=True)
            self.assertEqual(state, self.original)
            self.assertEqual(commands.call_count, 2)
            self.assertEqual(commands.call_args.args, ("ip", "-4", "route", "replace", "default", "via", "172.0.0.1",
                                                       "dev", "eth0", "proto", "dhcp", "metric", "100"))
            self.assertEqual(json.loads((path / "route.json").read_text()), self.original)
            self.assertIn("Прежний маршрут возвращён", err.getvalue())


if __name__ == "__main__":
    unittest.main()
