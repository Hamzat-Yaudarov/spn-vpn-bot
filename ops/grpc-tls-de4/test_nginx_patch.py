"""Offline DE4 exact-location render, filesystem, rollback and CLI guards."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("de4_nginx_patch", Path(__file__).with_name("nginx_patch.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SOURCE = Path(__file__).resolve().parents[1] / "direct-deeee1/nginx.conf"
# Public synthetic CDN location; no private snapshots or credentials are read.
CDN = b"""    location ^~ /assets/app/ {
        proxy_pass http://127.0.0.1:10085;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
"""


class NginxPatchTests(unittest.TestCase):
    def setUp(self):
        self.original = SOURCE.read_bytes().replace(m.ANCHOR, CDN + m.ANCHOR, 1)
        self.before_sha = m.sha(self.original)
        self.candidate = m.render(self.original, self.before_sha)
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site, self.backup = self.root / "site.conf", self.root / "backup"
        self.site.write_bytes(self.original)

    def fixture(self, *, already=False, existing=False, failure=None, rollback=False):
        self.site.write_bytes(self.candidate if already else self.original)
        events, failures = [], set()

        def command(args, **kwargs):
            events.append((args, self.site.read_bytes()))
            kind = "test" if "-t" in args else "reload" if "reload" in args else "active"
            if failure == kind and kind not in failures:
                failures.add(kind)
                if kind == "active":
                    return "inactive"
                raise m.PatchError("PRIVATE-OUTPUT")
            if failure == "admin" and kind == "test":
                self.site.write_bytes(b"ADMIN-EDIT-KEEP")
                raise m.PatchError("PRIVATE-OUTPUT")
            return "active" if kind == "active" else ""

        def write(path, raw, mode):
            path.write_bytes(raw)
            path.chmod(mode)

        patches = (
            patch.object(m, "SITE", self.site), patch.object(m, "BACKUP", self.backup),
            patch.object(m, "preflight", return_value=(self.site.read_bytes(), self.original, self.candidate, already)),
            patch.object(m, "directory"), patch.object(m, "read_regular", side_effect=lambda path, **kw: path.read_bytes()),
            patch.object(m, "write_exclusive", side_effect=write), patch.object(m.os, "fchown"),
            patch.object(m, "command", side_effect=command),
        )
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            if existing:
                m.backup(self.original, self.candidate, already_present=False)
            action = m.rollback if rollback else m.apply
            if failure:
                with self.assertRaises(m.PatchError):
                    action(m.TARGET, self.before_sha)
                result = None
            else:
                result = action(m.TARGET, self.before_sha)
        return result, events

    def test_default_plan_has_no_io_and_never_echoes_arguments(self):
        output = io.StringIO()
        with patch.object(m, "apply") as apply, patch.object(m, "rollback") as rollback, \
             patch.object(m, "read_regular") as read, patch.object(m, "command") as command, \
             contextlib.redirect_stdout(output):
            self.assertEqual(m.main(["--confirm-target", "SECRET", "--before-sha256", "SECRET"]), 0)
        for mocked in (apply, rollback, read, command):
            mocked.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        self.assertNotIn("SECRET", output.getvalue())

    def test_render_changes_only_exact_normal_mode_location(self):
        self.assertEqual(self.candidate.replace(m.BLOCK, b"", 1), self.original)
        self.assertEqual(self.candidate.count(m.BLOCK), 1)
        self.assertIn(b"location = /grpc-de4-tls-test/Tun {", m.BLOCK)
        self.assertIn(b"grpc_pass grpc://127.0.0.1:10089;", m.BLOCK)
        for value in (b"grpc_set_header Host deeee1.wayspn.com;", b"grpc_read_timeout 3600s;",
                      b"grpc_send_timeout 3600s;", b"grpc_connect_timeout 5s;", b"grpc_next_upstream off;",
                      b"client_max_body_size 0;", b"client_body_timeout 3600s;", b"access_log off;"):
            self.assertIn(value, m.BLOCK)
        for value in (b"TunMulti", b"rewrite", b"proxy_pass", b"proxy_buffering", b"proxy_request_buffering",
                      b"listen", b"ssl_certificate", b"systemctl", b"Connection", b"Upgrade"):
            self.assertNotIn(value, m.BLOCK)

    def test_sha_and_exact_classification_reject_all_drift(self):
        self.assertEqual(m.classify(self.original, self.before_sha), (self.original, self.candidate, False))
        self.assertEqual(m.classify(self.candidate, self.before_sha), (self.original, self.candidate, True))
        for raw in (self.original + b"\n", self.candidate + b"\n", self.candidate + m.BLOCK,
                    self.candidate.replace(b"grpc_send_timeout 3600s", b"grpc_send_timeout 60s")):
            with self.subTest(length=len(raw)), self.assertRaises(m.PatchError):
                m.classify(raw, self.before_sha)
        for value in (None, "", "x" * 64, "A" * 64, "0" * 63, self.before_sha + "\n"):
            with self.subTest(sha=value), self.assertRaises(m.PatchError):
                m.render(self.original, value)

    def test_missing_or_moved_strict_tls_ws_cdn_anchor_is_refused_even_with_matching_sha(self):
        for token in (b"ssl_reject_handshake on;", b"listen 443 ssl http2;", b"listen [::]:443 ssl http2;",
                      b"ssl_certificate_key /etc/letsencrypt/live/deeee1.wayspn.com/privkey.pem;",
                      b"if ($host != deeee1.wayspn.com)", b"if ($ssl_server_name != deeee1.wayspn.com)",
                      b"location ^~ /direct/ws/", b"proxy_pass http://127.0.0.1:10088;",
                      b"location ^~ /assets/app/", b"proxy_pass http://127.0.0.1:10085;", m.ANCHOR):
            raw = self.original.replace(token, b"CHANGED", 1)
            with self.subTest(token=token), self.assertRaises(m.PatchError):
                m.render(raw, m.sha(raw))
        for raw in (self.original + m.ANCHOR, self.original + b"# 10089\n"):
            with self.assertRaises(m.PatchError):
                m.render(raw, m.sha(raw))

    def test_target_must_match_actual_linux_root_interface_before_other_commands(self):
        with patch.object(m.sys, "platform", "linux"), patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m, "command", return_value=json.dumps([{"addr_info": [{"local": m.TARGET}]}])) as command:
            m.target_check(m.TARGET)
            command.reset_mock()
            with self.assertRaises(m.PatchError):
                m.target_check("144.31.168.231")
            command.assert_not_called()
        with patch.object(m.sys, "platform", "linux"), patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m, "command", return_value='[{"addr_info":[{"local":"192.0.2.1"}]}]'), \
             self.assertRaises(m.PatchError):
            m.target_check(m.TARGET)

    def test_apply_backs_up_privately_then_tests_and_gracefully_reloads(self):
        result, events = self.fixture()
        self.assertTrue(result["applied"])
        self.assertEqual(self.site.read_bytes(), self.candidate)
        self.assertEqual((self.backup / "nginx.before.conf").read_bytes(), self.original)
        self.assertEqual((self.backup / "nginx.candidate.conf").read_bytes(), self.candidate)
        self.assertEqual({p.name for p in self.backup.iterdir()}, m.FILES)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o700)
        for path in self.backup.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([args[1:] for args, raw in events], [["-t"], ["reload", "nginx"], ["is-active", "nginx"]])
        self.assertTrue(all(raw == self.candidate for args, raw in events))

    def test_failed_test_reload_or_liveness_restores_exact_original_and_reloads(self):
        for failure in ("test", "reload", "active"):
            self.backup = self.root / ("backup-" + failure)
            with self.subTest(failure=failure):
                self.fixture(failure=failure)
                self.assertEqual(self.site.read_bytes(), self.original)

    def test_idempotent_apply_requires_exact_complete_backup(self):
        with self.assertRaises(m.PatchError):
            self.fixture(already=True)
        result, _ = self.fixture(already=True, existing=True)
        self.assertTrue(result["already_present"])
        self.assertTrue(result["backup_reused"])

    def test_incomplete_or_modified_backups_are_never_overwritten(self):
        self.backup.mkdir(mode=0o700)
        sentinel = self.backup / "unrelated"
        sentinel.write_bytes(b"KEEP")
        with self.assertRaises(m.PatchError):
            self.fixture()
        self.assertEqual(sentinel.read_bytes(), b"KEEP")
        self.assertEqual(self.site.read_bytes(), self.original)

    def test_explicit_rollback_removes_only_owned_block_and_preserves_backup(self):
        result, events = self.fixture(already=True, existing=True, rollback=True)
        self.assertTrue(result["rolled_back"])
        self.assertEqual(self.site.read_bytes(), self.original)
        self.assertEqual((self.backup / "nginx.candidate.conf").read_bytes(), self.candidate)
        self.assertEqual([args[1:] for args, raw in events], [["-t"], ["reload", "nginx"], ["is-active", "nginx"]])

    def test_rollback_failure_restores_exact_candidate(self):
        for failure in ("test", "reload", "active"):
            self.backup = self.root / ("rollback-" + failure)
            with self.subTest(failure=failure):
                self.fixture(already=True, existing=True, rollback=True, failure=failure)
                self.assertEqual(self.site.read_bytes(), self.candidate)

    def test_already_removed_rollback_still_requires_backup_but_does_not_reload(self):
        result, events = self.fixture(existing=True, rollback=True)
        self.assertTrue(result["already_removed"])
        self.assertEqual(events, [])
        self.backup = self.root / "missing-backup"
        with self.assertRaises(FileNotFoundError):
            self.fixture(rollback=True)

    def test_concurrent_administrator_edit_is_never_overwritten(self):
        self.fixture(failure="admin")
        self.assertEqual(self.site.read_bytes(), b"ADMIN-EDIT-KEEP")
        self.assertEqual((self.backup / "nginx.before.conf").read_bytes(), self.original)

    def test_existing_temp_and_fresh_mismatch_are_never_overwritten(self):
        temporary = self.site.with_name("." + self.site.name + ".grpc-de4-tls-new")
        temporary.write_bytes(b"FOREIGN-KEEP")
        with patch.object(m, "SITE", self.site), patch.object(m, "read_regular", side_effect=lambda path, **kw: path.read_bytes()), \
             self.assertRaises(FileExistsError):
            m.atomic_site(self.candidate, expected=self.original)
        self.assertEqual(temporary.read_bytes(), b"FOREIGN-KEEP")
        with patch.object(m, "SITE", self.site), patch.object(m, "read_regular", return_value=b"ADMIN-EDIT"), \
             self.assertRaises(m.PatchError):
            m.atomic_site(self.candidate, expected=self.original)
        self.assertEqual(self.site.read_bytes(), self.original)

    def test_symlink_hardlink_and_nonprivate_directory_refused(self):
        link = self.root / "link"
        link.symlink_to(self.site)
        with self.assertRaises(m.PatchError):
            m.read_regular(link, mode=0o644)
        os.link(self.site, self.root / "hardlink")
        with self.assertRaises(m.PatchError):
            m.read_regular(self.site, mode=0o644)
        with self.assertRaises(m.PatchError):
            m.directory(self.root, private=True)

    def test_post_replace_fsync_failure_also_rolls_back(self):
        original_sync = m.sync_directory

        def sync(path):
            if path == self.site.parent and self.site.read_bytes() == self.candidate:
                raise OSError("PRIVATE-FSYNC")
            original_sync(path)

        with patch.object(m, "sync_directory", side_effect=sync):
            self.fixture(failure="fsync")
        self.assertEqual(self.site.read_bytes(), self.original)

    def test_cli_errors_are_sanitized_and_no_mutation_without_valid_sha(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error), patch.object(m, "command") as command, \
             patch.object(m, "target_check") as target:
            self.assertEqual(m.main(["--apply", "--confirm-target", m.TARGET]), 2)
            self.assertEqual(m.main(["--wrong", "TOKEN-SECRET"]), 2)
            self.assertEqual(m.main(["--apply", "--rollback"]), 2)
        command.assert_not_called()
        target.assert_not_called()
        self.assertNotIn("TOKEN-SECRET", error.getvalue())


if __name__ == "__main__":
    unittest.main()
