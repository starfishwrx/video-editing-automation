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

from orchestrator import vision_analyzer, vision_client  # noqa: E402


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

        def post(url, api_key, body, output, **kwargs):
            captured.update(url=url, auth=f"Bearer {api_key}", body=body, **kwargs)
            return vision_client.VisionCallResult(True, text='{"shots":[]}')

        with mock.patch.object(vision_client, "post_chat", side_effect=post):
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

    def test_final_content_and_thinking_contract_is_fixed(self):
        os.environ.update(ARK_API_KEY="synthetic", JY_VISION_THINKING="disabled",
                          JY_VISION_REASONING_EFFORT="high", JY_VISION_RETRIES="6")
        failed = vision_client.VisionCallResult(False, error_code="AUTHENTICATION", http_status=401)
        with mock.patch.object(vision_client, "post_chat", return_value=failed) as post:
            ok, text = vision_analyzer._run_volc_chat(None, "prompt", self.root / "out.txt", timeout=5)
        self.assertFalse(ok)
        self.assertIn("AUTHENTICATION", text)
        body = post.call_args.args[2]
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["max_tokens"], 16384)
        self.assertEqual(post.call_args.kwargs["max_attempts"], 2)
