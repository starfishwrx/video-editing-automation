"""Text selection keeps evidence gates across the GLM and Qwen HTTP routes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import cli, vision_analyzer, vision_client


class SentenceMatchingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sentence matching ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        clean = {key: value for key, value in os.environ.items()
                 if not key.startswith(("JY_VISION_", "JY_QWEN_"))}
        clean.update(ARK_API_KEY="synthetic-ark-key", JY_QWEN_API_KEY="synthetic-qwen-key",
                     JY_QWEN_MODEL="qwen-test", JY_QWEN_BASE_URL="https://example.invalid/v1",
                     JY_SLOT_DIR=str(self.root / "slots"))
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _match(self, response, *, route="volc_glm", manifest=None):
        os.environ["JY_VISION_TRANSPORT"] = route
        manifest = manifest or {"segments": [{"text": "吸收很快"}, {"text": "包装可爱"}]}
        analysis = {"shots": [{"shot_id": "S1", "status": "ready_for_matching",
                                "description": "倒蓝水演示吸收"}]}
        report = self.root / route
        with mock.patch.object(vision_client, "post_chat", return_value=response) as post, \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("provider process forbidden")), \
             mock.patch.object(subprocess, "run", side_effect=AssertionError("provider process forbidden")):
            result = vision_analyzer.match_sentences(manifest, analysis, report)
        record = json.loads((report / "shot_match_ai.json").read_text(encoding="utf-8"))
        return result, record, post

    def test_authentication_failure_blocks_once_with_structured_reason(self):
        for route in ("volc_glm", "qwen"):
            with self.subTest(route=route):
                response = vision_client.VisionCallResult(
                    False, error_code="AUTHENTICATION", http_status=401)
                result, record, post = self._match(response, route=route)
                self.assertEqual(post.call_count, 1)
                self.assertIn("AUTHENTICATION", result["semantic_match_error"])
                self.assertEqual(result["semantic_matches"], [])
                self.assertEqual(record["attempts"], 1)

    def test_rate_limit_does_not_create_an_outer_retry(self):
        response = vision_client.VisionCallResult(
            False, error_code="RATE_LIMIT", http_status=429, attempts=2)
        result, record, post = self._match(response)
        self.assertEqual(post.call_count, 1)
        self.assertIn("semantic_match_error", result)
        self.assertEqual(record["attempts"], 1)

    def test_empty_candidates_remain_blocked_without_repeating_judgment(self):
        response = vision_client.VisionCallResult(True, text=json.dumps(
            [{"index": 1, "top3": []}, {"index": 2, "top3": []}]))
        result, record, post = self._match(response)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["semantic_matches"], [])
        self.assertIn("semantic_match_error", result)
        self.assertEqual(record["attempts"], 1)

    def test_malformed_final_response_never_creates_matches(self):
        result, record, post = self._match(
            vision_client.VisionCallResult(True, text="unstructured answer"))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["semantic_matches"], [])
        self.assertIn("match response invalid", result["semantic_match_error"])
        self.assertEqual(record["attempts"], 1)

    def test_both_http_routes_rank_sentences_without_image_or_subprocess(self):
        matches = [{"index": 1, "top3": [{"shot_id": "S1", "score": 80}]}]
        response = vision_client.VisionCallResult(True, text=json.dumps(matches))
        for route, model in (("volc_glm", "glm-5.3-flash"), ("qwen", "qwen-test")):
            with self.subTest(route=route):
                result, record, post = self._match(response, route=route)
                self.assertEqual(result["semantic_matches"], matches)
                self.assertNotIn("semantic_match_error", result)
                self.assertEqual(record["attempts"], 1)
                self.assertNotIn("profile", record)
                self.assertNotIn("stderr_tail", record)
                body = post.call_args.args[2]
                self.assertEqual(body["model"], model)
                self.assertEqual([part["type"] for part in body["messages"][0]["content"]],
                                 ["text"])
                self.assertEqual(post.call_args.kwargs["max_attempts"], 2)
                self.assertTrue(post.call_args.kwargs["validator"](matches))

    def test_sentence_prompt_carries_the_claim_it_must_prove(self):
        manifest = {"category": "weipin", "segments": [
            {"text": "量大的姐妹真的闭眼冲"}, {"text": "包装可爱"}]}
        response = vision_client.VisionCallResult(True, text=json.dumps(
            [{"index": 1, "top3": [{"shot_id": "S1", "score": 80}]}]))
        _, _, post = self._match(response, manifest=manifest)
        prompt = post.call_args.args[2]["messages"][0]["content"][0]["text"]
        self.assertIn("1. 量大的姐妹真的闭眼冲（卖点：吸收容量——已吸水巾体，液体锁在中央不扩散）", prompt)
        self.assertIn("2. 包装可爱\n", prompt)
        self.assertIn("光从巾体透过来", prompt)
        self.assertIn("不得编造描述或选择列表以外的 shot_id", prompt)


class TemporaryMatchBlockTests(unittest.TestCase):
    def test_failed_ai_match_blocks_before_rule_fallback(self):
        analysis = {"shots": [{"shot_id": "S1", "status": "ready_for_matching"}],
                    "vision_analysis": {"errors": []}}

        def failed_match(_manifest, current, _report_dir):
            current["semantic_match_error"] = "match call vision request failed [AUTHENTICATION]"
            current["semantic_matches"] = []
            return current

        with tempfile.TemporaryDirectory() as raw, \
             mock.patch.object(cli, "_run_shot_analysis", return_value=analysis), \
             mock.patch.object(vision_analyzer, "match_sentences", side_effect=failed_match), \
             mock.patch.object(cli.shot_analyzer, "match_manifest") as rule_match:
            _, blocker = cli._temporary_match(
                {"segments": [], "selection_mode": "legacy"}, Path(raw) / "m.json", Path(raw))
        rule_match.assert_not_called()
        self.assertEqual(blocker["status"], "SHOT_MATCH_BLOCKED")
        self.assertIn("VISION_CALL_FAILED", blocker["message"])
        self.assertIn("AUTHENTICATION", blocker["error"])


if __name__ == "__main__":
    unittest.main()
