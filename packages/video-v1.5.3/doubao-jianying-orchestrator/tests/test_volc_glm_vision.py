import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import vision_analyzer  # noqa: E402


class VolcDirectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="volc_glm_")
        self.root = Path(self._tmp.name)
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("JY_VISION_") and k not in {
                     "ARK_API_KEY", "JY_VOLC_API_KEY", "DEEPSEEK_API_KEY"}}
        clean["JY_SLOT_DIR"] = str(self.root / "slots")
        self._env = mock.patch.dict(os.environ, clean, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_unset_transport_defaults_to_volc_glm(self):
        self.assertEqual(vision_analyzer._vision_transport(), "volc_glm")
        self.assertEqual(vision_analyzer._requested_batch_workers(), 20)
        self.assertTrue(vision_analyzer._direct_http_transport())

    def test_codex_rollback_keeps_single_worker(self):
        os.environ["JY_VISION_TRANSPORT"] = "codex"
        self.assertEqual(vision_analyzer._vision_transport(), "codex")
        self.assertEqual(vision_analyzer._requested_batch_workers(), 1)

    def test_direct_glm_posts_chat_completions_with_image(self):
        os.environ["ARK_API_KEY"] = "ark-test-key-value"
        sheet = self.root / "sheet.jpg"
        sheet.write_bytes(b"\xff\xd8\xff\xd9")
        captured = {}

        class _Resp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{\"shots\":[]}"}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def opener(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Resp()

        with mock.patch.object(vision_analyzer.urllib.request, "urlopen", side_effect=opener):
            ok, text = vision_analyzer._run_vision(
                sheet, "看图", self.root / "out.txt", profile="volc", timeout=30)
        self.assertTrue(ok)
        self.assertEqual(text, "{\"shots\":[]}")
        self.assertTrue(captured["url"].endswith("/api/plan/v3/chat/completions"))
        self.assertEqual(captured["auth"], "Bearer ark-test-key-value")
        body = captured["body"]
        self.assertEqual(body["model"], "glm-5.3-flash")
        self.assertEqual(body["thinking"]["type"], "enabled")
        image = body["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(image.startswith("data:image/jpeg;base64,"))
        self.assertEqual(vision_analyzer._requested_batch_workers(), 20)

    def test_auth_failure_is_not_retried_and_redacts_the_key(self):
        os.environ["ARK_API_KEY"] = "ark-test-key-value"
        sheet = self.root / "sheet.jpg"
        sheet.write_bytes(b"\xff\xd8\xff\xd9")

        def opener(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {},
                io.BytesIO(b'{"error":"bad ark-test-key-value"}'))

        with mock.patch.object(vision_analyzer.urllib.request, "urlopen", side_effect=opener) as opened, \
                mock.patch.object(vision_analyzer.time, "sleep"):
            ok, text = vision_analyzer._run_vision_with_retry(
                sheet, "看图", self.root / "out.txt",
                profile="volc", timeout=5)
        self.assertFalse(ok)
        self.assertIn("VOLC_HTTP_401", text)
        self.assertIn("ark-***", text)
        self.assertNotIn("ark-test-key-value", text)
        self.assertEqual(opened.call_count, 1)
