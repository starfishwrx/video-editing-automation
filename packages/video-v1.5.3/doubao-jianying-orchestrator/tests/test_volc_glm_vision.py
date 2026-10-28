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
                 if not k.startswith(("JY_VISION_", "JY_QWEN_")) and k not in {
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

    def test_text_route_uses_qwen_with_thinking_off(self):
        os.environ.update(
            ARK_API_KEY="ark-test-key-value",
            JY_VISION_TEXT_BASE_URL="https://example.invalid/compatible-mode/v1",
            JY_VISION_TEXT_API_KEY="sk-test-secret-value",
            JY_VISION_TEXT_MODEL="qwen3.8-flash",
        )
        sheet = self.root / "sheet.jpg"
        sheet.write_bytes(b"\xff\xd8\xff\xd9")
        captured = []

        def post(url, api_key, body, output, **kwargs):
            captured.append({"url": url, "api_key": api_key, "body": body})
            return vision_client.VisionCallResult(
                True, text='{"shot_id":"s1","readable_claims":[],"pointed_text":[]}')

        with mock.patch.object(vision_client, "post_chat", side_effect=post):
            ok, _text = vision_analyzer._run_text_chat(
                sheet, "读字", self.root / "text.txt", timeout=30)
            glm_ok, _glm = vision_analyzer._run_volc_chat(
                sheet, "看图", self.root / "glm.txt", timeout=30)
        self.assertTrue(ok)
        self.assertTrue(glm_ok)
        text_body = captured[0]["body"]
        self.assertEqual(text_body["model"], "qwen3.8-flash")
        self.assertIs(text_body["enable_thinking"], False)
        self.assertNotIn("thinking", text_body)
        self.assertNotIn("reasoning_effort", text_body)
        self.assertTrue(captured[0]["url"].endswith("/compatible-mode/v1/chat/completions"))
        self.assertEqual(captured[0]["api_key"], "sk-test-secret-value")
        glm_body = captured[1]["body"]
        self.assertEqual(glm_body["model"], "glm-5.3-flash")
        self.assertEqual(glm_body["thinking"], {"type": "enabled"})
        self.assertEqual(glm_body["reasoning_effort"], "low")
        self.assertTrue(captured[1]["url"].endswith("/api/plan/v3/chat/completions"))

    def test_qwen_transport_routes_vision_and_fails_closed_without_credentials(self):
        os.environ.update(JY_VISION_TRANSPORT="qwen", JY_QWEN_BASE_URL="https://example.invalid/v1",
                          JY_QWEN_MODEL="qwen-test", JY_QWEN_API_KEY="sk-test-key")
        sheet = self.root / "sheet.jpg"
        sheet.write_bytes(b"\xff\xd8\xff\xd9")
        with mock.patch.object(vision_client, "post_chat", return_value=vision_client.VisionCallResult(True, text='{"shots":[]}')) as post, \
                mock.patch.object(vision_analyzer, "_run_volc_chat") as glm:
            ok, _ = vision_analyzer._run_vision(sheet, "prompt", self.root / "out.txt", profile="test", timeout=30)
            self.assertTrue(ok)
            body = post.call_args.args[2]
            self.assertEqual(body["model"], "qwen-test")
            self.assertIs(body["enable_thinking"], False)
            self.assertTrue(body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            self.assertEqual(vision_analyzer._vision_summary("test", [], [], [], partial=False)["model"], "qwen-test")
            glm.assert_not_called()
        del os.environ["JY_QWEN_API_KEY"]
        with mock.patch.object(vision_client, "post_chat") as post:
            ok, error = vision_analyzer._run_vision(sheet, "prompt", self.root / "missing.txt", profile="test", timeout=30)
            self.assertFalse(ok)
            self.assertEqual(error, "QWEN_CONFIG_MISSING")
            post.assert_not_called()

    def test_qwen_cache_identity_binds_model_and_endpoint(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"test")
        shot = {"video": str(video), "source_start": 0, "source_end": 1}
        os.environ.update(JY_VISION_TRANSPORT="qwen", JY_QWEN_BASE_URL="https://example.invalid/v1",
                          JY_QWEN_MODEL="qwen-a", JY_QWEN_API_KEY="sk-test-key")
        a = vision_analyzer._vision_cache_key({"product": "test"}, shot, "test")
        os.environ["JY_QWEN_MODEL"] = "qwen-b"
        b = vision_analyzer._vision_cache_key({"product": "test"}, shot, "test")
        os.environ["JY_QWEN_BASE_URL"] = "https://other.invalid/v1"
        c = vision_analyzer._vision_cache_key({"product": "test"}, shot, "test")
        self.assertEqual(len({a, b, c}), 3)
