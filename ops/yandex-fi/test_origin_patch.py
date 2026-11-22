"""Synthetic origin installation/rollback tests. No server or Nginx calls."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("origin_patch_under_test", Path(__file__).with_name("origin_patch.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
SOURCE = Path(__file__).resolve().parents[1] / "direct-fi/nginx.conf"


class OriginPatchTests(unittest.TestCase):
    def setUp(self):
        self.original = SOURCE.read_bytes()
        self.candidate = m.render(self.original)
        self.temp = tempfile.TemporaryDirectory(dir="/private/tmp"); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site, self.backup = self.root / "vhost.conf", self.root / "backup"
        self.site.write_bytes(self.original)

    def test_default_is_pure_even_with_untrusted_target_argument(self):
        output = io.StringIO()
        with patch.object(m, "apply") as apply, patch.object(m, "read_regular") as read, \
             patch.object(m, "command") as command, contextlib.redirect_stdout(output):
            self.assertEqual(m.main(["--confirm-target", "SECRET"]), 0)
        apply.assert_not_called(); read.assert_not_called(); command.assert_not_called()
        self.assertTrue(json.loads(output.getvalue())["plan_only"])
        self.assertNotIn("SECRET", output.getvalue())

    def test_exact_hash_adds_only_location_and_preserves_all_original_bytes(self):
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), m.BEFORE_SHA256)
        self.assertEqual(self.candidate.replace(m.BLOCK, b"", 1).replace(m.HEADERS, b"", 1), self.original)
        self.assertEqual(self.candidate.count(b"location ^~ /assets/app/"), 1)
        self.assertEqual(self.candidate.count(m.HEADERS), 1)
        self.assertIn(b"client_header_buffer_size 8k;", m.HEADERS)
        self.assertIn(b"large_client_header_buffers 4 8k;", m.HEADERS)
        self.assertNotIn(m.HEADERS, m.BLOCK)
        self.assertEqual(m.TARGET, "92.42.102.103")
        self.assertEqual(m.DOMAIN, "fi103.wayspn.com")
        self.assertIn(b"proxy_pass http://127.0.0.1:10085;", m.BLOCK)
        for directive in (b"proxy_buffering off;", b"proxy_request_buffering off;", b"proxy_cache off;",
                          b'proxy_set_header Connection "";', b'proxy_set_header Forwarded "";',
                          b"proxy_set_header Host fi103.wayspn.com;", b"proxy_read_timeout 3600s;",
                          b"proxy_send_timeout 3600s;", b"add_header Cache-Control no-store always;", b"access_log off;"):
            self.assertIn(directive, m.BLOCK)
        self.assertIn(b"proxy_pass http://127.0.0.1:10088;", self.candidate)
        self.assertEqual(m.classify(self.original), (self.original, self.candidate, False))
        self.assertEqual(m.classify(self.candidate), (self.original, self.candidate, True))

    def test_any_drift_duplicate_location_or_modified_block_is_refused(self):
        for value in (self.original + b"\n", self.candidate + b"\n", self.candidate + m.BLOCK,
                      self.candidate.replace(b"proxy_request_buffering off;", b"proxy_request_buffering on;"),
                      self.candidate.replace(b"/direct/ws/", b"/changed/ws/")):
            with self.subTest(length=len(value)), self.assertRaises(m.OriginError): m.classify(value)

    def test_fixed_target_checked_before_commands_and_only_correct_interface_allowed(self):
        with patch.object(m.sys, "platform", "linux"), patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m, "command", return_value=json.dumps([{"addr_info": [{"local": m.TARGET}]}])) as command:
            m.target_check(m.TARGET)
            command.reset_mock()
            with self.assertRaises(m.OriginError): m.target_check("151.242.161.117")
            command.assert_not_called()
        with patch.object(m.sys, "platform", "linux"), patch.object(m.os, "geteuid", return_value=0), \
             patch.object(m, "command", return_value='[{"addr_info":[{"local":"151.242.161.117"}]}]'), self.assertRaises(m.OriginError):
            m.target_check(m.TARGET)

    def fixture(self, *, already=False, failure=None, existing=False):
        if already: self.site.write_bytes(self.candidate)
        events = []
        failures = set()
        def command(args, **kwargs):
            events.append((args, self.site.read_bytes()))
            kind = "test" if "-t" in args else "reload" if "reload" in args else "active"
            if failure == kind and kind not in failures:
                failures.add(kind)
                if kind == "active": return "inactive"
                raise m.OriginError("PRIVATE-OUTPUT")
            return "active" if kind == "active" else ""
        def write(path, raw, mode): path.write_bytes(raw); path.chmod(mode)
        patches = [patch.object(m, "SITE", self.site), patch.object(m, "BACKUP", self.backup),
                   patch.object(m, "preflight", return_value=(self.candidate if already else self.original,
                                self.original, self.candidate, already)),
                   patch.object(m, "directory"), patch.object(m, "read_regular", side_effect=lambda path, **kw: path.read_bytes()),
                   patch.object(m, "write_exclusive", side_effect=write), patch.object(m.os, "fchown"),
                   patch.object(m, "command", side_effect=command)]
        with contextlib.ExitStack() as stack:
            for item in patches: stack.enter_context(item)
            if existing: m.backup(self.original, self.candidate, already_present=False)
            if failure:
                with self.assertRaises(m.OriginError): m.apply(m.TARGET)
                result = None
            else: result = m.apply(m.TARGET)
        return result, events

    def test_apply_exclusive_backup_atomic_candidate_test_then_reload(self):
        result, events = self.fixture()
        self.assertTrue(result["applied"]); self.assertFalse(result["already_present"])
        self.assertEqual(self.site.read_bytes(), self.candidate)
        self.assertEqual((self.backup / "nginx.before.conf").read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o700)
        for file in self.backup.iterdir(): self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual([row[0][1:] for row in events], [["-t"], ["reload", "nginx"], ["is-active", "nginx"]])
        self.assertTrue(all(raw == self.candidate for _, raw in events))
        self.assertFalse(self.site.with_name("." + self.site.name + ".yandex-origin-new").exists())

    def test_new_test_reload_or_liveness_failure_restores_exact_original_and_reloads(self):
        for failure in ("test", "reload", "active"):
            with self.subTest(failure=failure):
                # New independent root so each backup remains exclusive.
                self.backup = self.root / ("backup-" + failure)
                self.site.write_bytes(self.original)
                _, events = self.fixture(failure=failure)
                self.assertEqual(self.site.read_bytes(), self.original)
                self.assertEqual(events[-3][0], ["/usr/sbin/nginx", "-t"])
                self.assertEqual(events[-2][0], ["/usr/bin/systemctl", "reload", "nginx"])
                self.assertEqual(events[-1][0], ["/usr/bin/systemctl", "is-active", "nginx"])
                self.assertTrue(all(raw == self.original for _, raw in events[-3:]))

    def test_already_candidate_requires_matching_complete_backup_then_only_reloads(self):
        with self.assertRaises(m.OriginError): self.fixture(already=True)
        result, events = self.fixture(already=True, existing=True)
        self.assertTrue(result["already_present"]); self.assertTrue(result["backup_reused"])
        self.assertEqual((self.backup / "nginx.before.conf").read_bytes(), self.original)
        self.assertTrue(all(raw == self.candidate for _, raw in events))

    def test_backup_drift_or_incomplete_backup_never_overwritten(self):
        self.backup.mkdir(mode=0o700)
        sentinel = self.backup / "unrelated"; sentinel.write_bytes(b"KEEP")
        with self.assertRaises(m.OriginError): self.fixture()
        self.assertEqual(sentinel.read_bytes(), b"KEEP")
        self.assertEqual(self.site.read_bytes(), self.original)

    def test_existing_temp_is_not_removed_or_overwritten(self):
        temporary = self.site.with_name("." + self.site.name + ".yandex-origin-new")
        temporary.write_bytes(b"KEEP")
        with patch.object(m, "SITE", self.site), patch.object(m.os, "fchown"), self.assertRaises(FileExistsError):
            m.atomic_site(self.candidate)
        self.assertEqual(temporary.read_bytes(), b"KEEP")
        self.assertEqual(self.site.read_bytes(), self.original)

    def test_new_foreign_temp_after_replace_is_never_unlinked(self):
        temporary = self.site.with_name("." + self.site.name + ".yandex-origin-new")
        def sync(path): temporary.write_bytes(b"FOREIGN-KEEP")
        with patch.object(m, "SITE", self.site), patch.object(m.os, "fchown"), patch.object(m, "sync_directory", side_effect=sync):
            m.atomic_site(self.candidate)
        self.assertEqual(temporary.read_bytes(), b"FOREIGN-KEEP")
        self.assertEqual(self.site.read_bytes(), self.candidate)

    def test_read_regular_rejects_symlinks_and_hardlinks(self):
        link = self.root / "symlink"; link.symlink_to(self.site)
        with self.assertRaises(m.OriginError): m.read_regular(link, mode=0o644)
        os.link(self.site, self.root / "hardlink")
        with self.assertRaises(m.OriginError): m.read_regular(self.site, mode=0o644)

    def test_fsync_failure_after_replace_is_rolled_back(self):
        original_sync = m.sync_directory
        calls = []
        def sync(path):
            calls.append(path)
            # Backup fsyncs happen first. Fail exactly after candidate replace.
            if path == self.site.parent and self.site.read_bytes() == self.candidate:
                raise OSError("PRIVATE-FSYNC")
            return original_sync(path)
        with patch.object(m, "sync_directory", side_effect=sync):
            _, events = self.fixture(failure="fsync")
        self.assertEqual(self.site.read_bytes(), self.original)
        self.assertEqual(events[-1][1], self.original)

    def test_error_messages_never_echo_raw_output_or_mistyped_credentials(self):
        error = io.StringIO()
        with patch.object(m, "apply", side_effect=RuntimeError("TOKEN-SECRET")), contextlib.redirect_stderr(error):
            self.assertEqual(m.main(["--apply", "--confirm-target", m.TARGET]), 2)
            self.assertEqual(m.main(["--wrong", "TOKEN-SECRET"]), 2)
        self.assertNotIn("TOKEN-SECRET", error.getvalue())


if __name__ == "__main__": unittest.main()
