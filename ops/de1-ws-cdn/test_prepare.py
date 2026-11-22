import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("de1_prepare", HERE / "prepare.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PreparationGuards(unittest.TestCase):
    def test_private_bundle_and_matching_unpredictable_path(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = prepare.prepare(Path(directory) / "stage")
            state = prepare.check_stage(stage)
            self.assertRegex(state["wsPath"], r"^/ws/[0-9a-f]{48}/$")
            self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600
                                for p in stage.rglob("*") if p.is_file()))

    def test_existing_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = prepare.prepare(Path(directory) / "stage")
            before = (stage / "state.json").read_bytes()
            with self.assertRaises(FileExistsError):
                prepare.prepare(stage)
            self.assertEqual((stage / "state.json").read_bytes(), before)

    def test_tampered_origin_path_and_public_or_credentialed_probe_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = prepare.prepare(Path(directory) / "stage")
            state = prepare.check_stage(stage)
            for key, value in (("domain", "other.example"), ("originIp", "127.0.0.1"),
                               ("wsPath", "/ws/\nmalformed")):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    prepare.validate_state({**state, key: value})
            original = (stage / "configprofile.json").read_text()
            for field, value in (("listen", "0.0.0.0"), ("settings", {"clients": [{}]})):
                profile = json.loads(original)
                profile["inbounds"][0][field] = value
                (stage / "configprofile.json").write_text(json.dumps(profile))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    prepare.check_stage(stage)


if __name__ == "__main__":
    unittest.main()
