import os
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import draft_visual_qc, runtime_policy, vision_analyzer, vision_client  # noqa: E402
from orchestrator.json_extract import (EmptyResponseError, extract_json,  # noqa: E402
                                       iter_json_values)


class _RequestEnv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="vision_retry_")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("JY_VISION_")}
        clean["JY_SLOT_DIR"] = self._tmp.name
        clean.update(ARK_API_KEY="synthetic-key", JY_STAGE_DEADLINE_TS="",
                     JY_TASK_DEADLINE_TS="", JY_CONCURRENCY_POLICY_FILE="")
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
                timeout=1, **kwargs)
        return result, call


class VisionRetryTests(_RequestEnv):
    def test_http_wrapper_does_not_multiply_transport_attempts(self):
        (ok, text), call = self._call([(False, "HTTP 429 Too Many Requests"), (True, "[]")])
        self.assertFalse(ok)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs["max_attempts"], 2)
        self.sleep.assert_not_called()

    def test_explicit_no_retry_reaches_the_transport(self):
        (ok, _), call = self._call([(False, "502")], max_retries=0)
        self.assertFalse(ok)
        self.assertEqual(call.call_args.kwargs["max_attempts"], 1)

    def test_legacy_retry_env_cannot_expand_attempt_budget(self):
        os.environ["JY_VISION_RETRIES"] = "6"
        (_, _), call = self._call([(False, "503")])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs["max_attempts"], 2)

    def test_empty_final_answer_never_succeeds(self):
        (ok, text), call = self._call([(True, "  ")])
        self.assertFalse(ok)
        self.assertTrue(vision_analyzer.is_empty_failure(text))
        self.assertEqual(call.call_count, 1)

    def test_validator_and_logical_attempt_callback_reach_single_transport_call(self):
        validator = lambda data: isinstance(data, list)
        callback = mock.Mock()
        (ok, _), call = self._call([(True, "[]")], validator=validator, on_attempt=callback)
        self.assertTrue(ok)
        self.assertIs(call.call_args.kwargs["validator"], validator)
        callback.assert_called_once_with(1, 2, "")
        for obsolete in ("profile", "workdir", "schema", "require_slot"):
            self.assertNotIn(obsolete, call.call_args.kwargs)

    def test_shared_http_client_owns_retries_slots_and_recovery_budget(self):
        from test_vision_client import LocalHTTP, answer
        acquisitions = []

        @contextmanager
        def client_slot(timeout=None):
            acquisitions.append(timeout)
            yield SimpleNamespace(acquired=True, waited=0.0)

        output = Path(self._tmp.name) / "response.json"
        with LocalHTTP({"status": 503}, {"payload": answer("[]")}) as server, \
             mock.patch.object(vision_client, "vision_slot", client_slot), \
             mock.patch.object(vision_client.random, "uniform", return_value=0), \
             mock.patch.object(runtime_policy, "vision_slot", side_effect=AssertionError("outer slot forbidden")):
            os.environ["JY_VOLC_BASE_URL"] = server.url.rsplit("/chat/completions", 1)[0]
            first = vision_analyzer._run_vision_with_retry(
                None, "prompt", output, timeout=2, validator=lambda data: isinstance(data, list))
            second = vision_analyzer._run_vision_with_retry(
                None, "prompt", output, timeout=2, validator=lambda data: isinstance(data, list))
        self.assertEqual(first, (True, "[]"))
        self.assertEqual(second, first)
        self.assertEqual(len(server.calls), 2)
        self.assertEqual(len(acquisitions), 2)
        ledger = json.loads(output.with_suffix(".call.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["attempts"], 2)
        self.assertEqual(ledger["attempt_limit"], 2)

    def test_authentication_failure_has_one_http_attempt_and_no_outer_retry(self):
        from test_vision_client import LocalHTTP, available_slot
        output = Path(self._tmp.name) / "authentication.json"
        with LocalHTTP({"status": 401}) as server, \
             mock.patch.object(vision_client, "vision_slot", available_slot):
            os.environ["JY_VOLC_BASE_URL"] = server.url.rsplit("/chat/completions", 1)[0]
            ok, text = vision_analyzer._run_vision_with_retry(None, "prompt", output, timeout=2)
        self.assertFalse(ok)
        self.assertIn("AUTHENTICATION", text)
        self.assertEqual(len(server.calls), 1)


class QcEmptyResponseTests(_RequestEnv):
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
