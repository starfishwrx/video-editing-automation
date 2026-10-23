import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import draft_visual_qc, vision_analyzer  # noqa: E402
from orchestrator.json_extract import (EmptyResponseError, extract_json,  # noqa: E402
                                       iter_json_values)


class _SlotEnv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="vision_retry_")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("JY_VISION_")}
        clean["JY_SLOT_DIR"] = self._tmp.name
        self._env = mock.patch.dict(os.environ, clean, clear=True)
        self._env.start()
        self._sleep = mock.patch.object(vision_analyzer.time, "sleep")
        self.sleep = self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _call(self, side_effect, **kwargs):
        with mock.patch.object(vision_analyzer, "_run_vision", side_effect=side_effect) as call:
            result = vision_analyzer._run_vision_with_retry(
                Path("sheet.jpg"), "prompt", Path("response.txt"),
                profile="volc", timeout=1, **kwargs)
        return result, call


class VisionRetryTests(_SlotEnv):
    def test_rate_limit_then_success(self):
        (ok, text), call = self._call([(False, "HTTP 429 Too Many Requests"), (True, "[]")])
        self.assertEqual((ok, text), (True, "[]"))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_empty_output_is_retried(self):
        (ok, text), call = self._call([(True, "  \n"), (True, '{"shots": []}')])
        self.assertTrue(ok)
        self.assertEqual(text, '{"shots": []}')
        self.assertEqual(call.call_count, 2)

    def test_exhausted_retries_return_last_failure(self):
        (ok, text), call = self._call([(False, "503 Service Unavailable")] * 10)
        self.assertFalse(ok)
        self.assertEqual(text, "503 Service Unavailable")
        self.assertEqual(call.call_count, 4)
        self.assertEqual(self.sleep.call_count, 3)

    def test_exhausted_empty_reports_empty_failure(self):
        os.environ["JY_VISION_RETRIES"] = "2"
        (ok, text), call = self._call([(True, "")] * 5)
        self.assertFalse(ok)
        self.assertTrue(vision_analyzer.is_empty_failure(text))
        self.assertEqual(call.call_count, 2)

    def test_deterministic_failure_is_not_retried(self):
        (ok, text), call = self._call([(False, "CCSWITCH_ANTHROPIC_CONFIG_MISSING")])
        self.assertEqual((ok, text), (False, "CCSWITCH_ANTHROPIC_CONFIG_MISSING"))
        self.assertEqual(call.call_count, 1)
        self.sleep.assert_not_called()

    def test_explicit_max_retries_overrides_env(self):
        (ok, _), call = self._call([(False, "502")] * 5, max_retries=0)
        self.assertFalse(ok)
        self.assertEqual(call.call_count, 1)

    def test_backoff_is_exponential_jittered_and_capped(self):
        os.environ["JY_VISION_BACKOFF"] = "3"
        for attempt, nominal in ((0, 3.0), (1, 6.0), (2, 12.0)):
            delay = vision_analyzer._backoff_seconds(attempt)
            self.assertGreaterEqual(delay, nominal * 0.75)
            self.assertLessEqual(delay, nominal * 1.25)
        self.assertLessEqual(vision_analyzer._backoff_seconds(10), 60.0)

    def test_slot_timeout_degrades_to_unthrottled_call(self):
        os.environ["JY_VISION_SLOTS"] = "1"
        os.environ["JY_VISION_SLOT_TIMEOUT"] = "0"
        with vision_analyzer._vision_slot() as held:
            self.assertTrue(held.acquired)
            (ok, text), call = self._call([(True, "[]")])
        self.assertTrue(ok)
        self.assertEqual(call.call_count, 1)


class QcEmptyResponseTests(_SlotEnv):
    def test_empty_qc_response_retried_once_then_failed(self):
        with mock.patch.object(vision_analyzer, "_run_vision",
                               side_effect=[(True, ""), (True, "")]) as call:
            ok, text = draft_visual_qc._call_qc_model(
                vision_analyzer, Path("sheet.jpg"), "prompt", Path("r.json"),
                timeout=1, workdir=Path("."))
        self.assertFalse(ok)
        self.assertIn("EMPTY", text)
        self.assertEqual(call.call_count, 2)

    def test_empty_then_valid_qc_response(self):
        body = '{"segments":[{"index":1,"match_level":"DIRECT","visual_ok":true}],"overall":"PASS"}'
        with mock.patch.object(vision_analyzer, "_run_vision",
                               side_effect=[(True, " "), (True, body)]):
            ok, text = draft_visual_qc._call_qc_model(
                vision_analyzer, Path("sheet.jpg"), "prompt", Path("r.json"),
                timeout=1, workdir=Path("."))
        self.assertTrue(ok)
        self.assertEqual(draft_visual_qc._parse_model(text)["overall"], "PASS")

    def test_empty_text_never_parses_as_pass(self):
        with self.assertRaises(ValueError):
            draft_visual_qc._parse_model("")


class JsonExtractTests(unittest.TestCase):
    def test_fenced_json(self):
        text = "结果如下：\n```json\n[{\"shot_id\": \"S1\"}]\n```\n以上。"
        self.assertEqual(extract_json(text), [{"shot_id": "S1"}])

    def test_bare_array_and_object(self):
        self.assertEqual(extract_json('[{"a": 1}]'), [{"a": 1}])
        self.assertEqual(extract_json('{"shots": []}'), {"shots": []})

    def test_json_followed_by_prose(self):
        text = '{"segments": [{"index": 1}], "overall": "PASS"}\n说明：第一段画面清晰 {不是 JSON}'
        self.assertEqual(extract_json(text)["overall"], "PASS")

    def test_prose_brackets_before_json_are_skipped(self):
        text = '注意[1]：以下为结果 {"shots": [{"shot_id": "S2"}]}'
        self.assertEqual(extract_json(text), {"shots": [{"shot_id": "S2"}]})

    def test_empty_and_invalid(self):
        with self.assertRaises(EmptyResponseError):
            extract_json("  ")
        with self.assertRaises(ValueError):
            extract_json("模型没有给出结构化结果")

    def test_unclosed_fence(self):
        self.assertEqual(extract_json('```json\n{"a": [1]}'), {"a": [1]})

    def test_vision_wrapper_keeps_contract(self):
        self.assertEqual(vision_analyzer._json_from_text('```\n{"shots": [1]}\n```'), {"shots": [1]})
        with self.assertRaises(ValueError):
            vision_analyzer._json_from_text("")

    def test_iter_values_for_qc_merge(self):
        text = '[{"index": 1, "match_level": "DIRECT"}]\n{"segments": [{"index": 1, "match_level": "DIRECT"}]}'
        self.assertEqual(len(list(iter_json_values(text))), 2)
        parsed = draft_visual_qc._parse_model(text)
        self.assertEqual([row["index"] for row in parsed["segments"]], [1])

    def test_qc_parse_fenced_with_prose(self):
        text = ('好的，验收结果：\n```json\n{"segments":[{"index":1,"match_level":"MISMATCH",'
                '"visual_ok":false}],"overall":"FAIL"}\n```\n补充说明：第1段不匹配。')
        parsed = draft_visual_qc._parse_model(text)
        self.assertEqual(parsed["overall"], "FAIL")
        self.assertEqual(parsed["segments"][0]["match_level"], "MISMATCH")


if __name__ == "__main__":
    unittest.main()
