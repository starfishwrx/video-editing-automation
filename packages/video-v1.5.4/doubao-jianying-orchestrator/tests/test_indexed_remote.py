"""Remote adapter tests with entirely fake transports and isolated slots."""
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import indexed_remote as remote


class IndexedRemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = mock.patch.dict(os.environ, {"JY_SLOT_DIR": str(self.root / "slots"),
            "JY_VISION_SLOTS": "2", "JY_INDEXED_REMOTE_SLOT_TIMEOUT": "0.01"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.signature = mock.patch.object(remote, "_model_signature", return_value={"model": "offline-test", "revision": "frozen"})
        self.signature.start()
        self.addCleanup(self.signature.stop)
        self.image = self.root / "frame.jpg"
        Image.new("RGB", (120, 200), "blue").save(self.image)
        self.request = {"stage": "observe", "prompt": "只检查给定动作", "payload": {"sentence": "擦干净"},
                        "images": [{"path": str(self.image), "timestamp_us": 500_000,
                                    "rendered_timestamp_us": 200_000, "index": 1}],
                        "output": str(self.root / "report" / "reply.raw.json")}
        self.model = remote.RemoteModel(profile="test", report_dir=self.root / "report")

    def test_contact_sheet_real_frames_and_content_cache(self):
        def fake(sheet, prompt, output, **kwargs):
            with Image.open(sheet) as image:
                self.assertGreaterEqual(image.height, 400)
                self.assertGreaterEqual(image.width, 320)
            self.assertIn('"frame_number":1', prompt)
            self.assertIn('"timestamp_us":500000', prompt)
            self.assertEqual(kwargs["timeout"], 120)
            return True, '```json\n{"observation":"擦净","status":"complete"}\n```'
        with mock.patch.object(remote.vision, "_run_vision", side_effect=fake) as transport:
            first = self.model(self.request)
            second = self.model(self.request)
        self.assertEqual(first, second)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.model.stats["remote_requests"], 1)
        self.assertEqual(self.model.stats["cache_hits"], 1)

    def test_changed_image_bytes_invalidates_cache(self):
        with mock.patch.object(remote.vision, "_run_vision", return_value=(True, '{"observed":true}')) as transport:
            self.model(self.request)
            Image.new("RGB", (120, 200), "red").save(self.image)
            self.model(self.request)
        self.assertEqual(transport.call_count, 2)

    def test_slot_timeout_does_not_bypass_throttle(self):
        @contextlib.contextmanager
        def denied(name, capacity, **kwargs):
            yield SimpleNamespace(acquired=name.startswith("indexed-request-"), waited=0.01)
        with mock.patch.object(remote, "slot", side_effect=denied), \
                mock.patch.object(remote.vision, "_run_vision") as transport:
            with self.assertRaises(TimeoutError):
                self.model(self.request)
        transport.assert_not_called()
        self.assertEqual(self.model.stats["remote_requests"], 0)
        self.assertEqual(self.model.stats["attempts"], 0)

    def test_invalid_json_retries_but_transport_failure_never_cached(self):
        with mock.patch.object(remote.vision, "_run_vision", side_effect=[
                (True, "not json"), (False, "timeout")]) as transport:
            with self.assertRaises(RuntimeError):
                self.model(self.request)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(list((self.root / "report" / "remote_cache").glob("*.json")), [])
        with mock.patch.object(remote.vision, "_run_vision", return_value=(True, '{"ok":true}')):
            self.assertEqual(self.model(self.request), {"ok": True})
        self.assertEqual(self.model.stats["remote_requests"], 3)

    def test_text_only_uses_file_transport(self):
        request = {**self.request, "images": [], "stage": "group"}
        def command(sheet, output, profile, **kwargs):
            self.assertIsNone(sheet)
            return ["offline-codex", str(output)]
        def run(args, prompt_path, stdout_path, stderr_path, timeout):
            Path(args[1]).write_text('```json\n{"groups":[]}\n```', encoding="utf-8")
            self.assertIn("擦干净", prompt_path.read_text(encoding="utf-8"))
            self.assertEqual(timeout, 120)
            return True, 0, ""
        with mock.patch.object(remote.vision, "_codex_command", side_effect=command), \
                mock.patch.object(remote.vision, "_run_codex_file", side_effect=run), \
                mock.patch.object(remote.vision, "_run_vision") as vision:
            self.assertEqual(self.model(request), {"groups": []})
        vision.assert_not_called()

    def test_missing_or_changed_evidence_fails_before_remote_call(self):
        request = {**self.request, "images": [{"path": str(self.image), "sha256": "incorrect"}]}
        with mock.patch.object(remote.vision, "_run_vision") as transport:
            with self.assertRaises(ValueError):
                self.model(request)
            self.image.unlink()
            with self.assertRaises(ValueError):
                self.model(self.request)
        transport.assert_not_called()

    def test_model_signature_change_does_not_reuse_response(self):
        with mock.patch.object(remote.vision, "_run_vision", return_value=(True, '{"ok":true}')) as transport:
            self.model(self.request)
            with mock.patch.object(remote, "_model_signature", return_value={"model": "updated-model"}):
                self.model(self.request)
        self.assertEqual(transport.call_count, 2)

    def test_distinct_arm_temp_directories_share_global_slot_and_capacity_two(self):
        observed = []
        @contextlib.contextmanager
        def capture(name, capacity, **kwargs):
            if name == "vision":
                observed.append((capacity, kwargs["slot_dir"]))
            yield SimpleNamespace(acquired=True, waited=0.0)
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "local-appdata"),
                                          "JY_VISION_SLOTS": "99"}):
            os.environ.pop("JY_SLOT_DIR", None)
            with mock.patch.object(remote, "slot", side_effect=capture), \
                    mock.patch.object(remote.vision, "_run_vision", return_value=(True, '{"ok":true}')):
                for arm in ("a", "b"):
                    with mock.patch.dict(os.environ, {"TEMP": str(self.root / arm / "temp"),
                                                      "TMP": str(self.root / arm / "tmp")}):
                        request = {**self.request, "output": str(self.root / arm / "response.json")}
                        self.model(request)
        expected = (self.root / "local-appdata" / "doubao-jianying" / "indexed_remote_slots").resolve()
        self.assertEqual(observed, [(2, expected), (2, expected)])


if __name__ == "__main__":
    unittest.main()
