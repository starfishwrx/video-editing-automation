import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import cli, vision_analyzer  # noqa: E402


class _Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex invocation ")
        self.root = Path(self._tmp.name)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("JY_VISION_")}
        clean["JY_SLOT_DIR"] = str(self.root / "slots")
        self._env = mock.patch.dict(os.environ, clean, clear=True)
        self._env.start()
        self._sleep = mock.patch.object(vision_analyzer.time, "sleep")
        self.sleep = self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        self._env.stop()
        self._tmp.cleanup()


@unittest.skipUnless(os.name == "nt", "cmd.exe wrapper is Windows-only")
class SpacedPathTests(_Env):
    def test_paths_with_spaces_reach_codex_as_single_arguments(self):
        shim = self.root / "shim"
        shim.mkdir()
        (shim / "codex.cmd").write_text(
            f'@"{sys.executable}" -c "import sys,json;print(json.dumps(sys.argv[1:]))" %*\r\n',
            encoding="utf-8")
        os.environ["PATH"] = str(shim) + os.pathsep + os.environ.get("PATH", "")
        work = self.root / "task dir"
        work.mkdir()
        response = work / "response.txt"
        prompt = work / "prompt.txt"
        prompt.write_text("hi", encoding="utf-8")
        ok, code, error = vision_analyzer._run_codex_file(
            vision_analyzer._codex_command(None, response, "volc", workdir=work),
            prompt, work / "stdout.log", work / "stderr.log", 30)
        self.assertEqual((ok, code, error), (True, 0, ""))
        argv = json.loads((work / "stdout.log").read_text(encoding="utf-8").strip())
        self.assertEqual(argv[argv.index("-o") + 1], str(response))
        self.assertEqual(argv[argv.index("-C") + 1], str(work))
        self.assertEqual(argv[-1], "-")


def _fake_codex(outcomes):
    """Each outcome is (returncode, stderr_text, response_text_or_None)."""
    queue = list(outcomes)

    def run(_args, _prompt, stdout_path, stderr_path, _timeout):
        code, stderr, response = queue.pop(0)
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        if response is not None:
            (stdout_path.parent / "response.txt").write_text(response, encoding="utf-8")
        return code == 0, code, ""
    return run


class MatchFailureTests(_Env):
    def _match(self, outcomes):
        manifest = {"segments": [{"text": "吸收很快"}, {"text": "包装可爱"}]}
        analysis = {"shots": [{"shot_id": "S1", "status": "ready_for_matching",
                               "description": "倒蓝水演示吸收"}]}
        with mock.patch.object(vision_analyzer, "_run_codex_file",
                               side_effect=_fake_codex(outcomes)) as call:
            result = vision_analyzer.match_with_codex(manifest, analysis, self.root / "report")
        record = json.loads((self.root / "report" / "shot_match_ai.json").read_text(encoding="utf-8"))
        return result, record, call

    def test_deterministic_cli_error_fails_once_with_real_stderr(self):
        result, record, call = self._match(
            [(2, "error: unexpected argument 'infar\\response.txt' found", None)])
        self.assertEqual(call.call_count, 1)
        self.assertIn("exit 2", result["semantic_match_error"])
        self.assertIn("unexpected argument", result["semantic_match_error"])
        self.assertEqual(result["semantic_matches"], [])
        self.assertEqual(record["attempts"], 1)
        self.assertIn("unexpected argument", record["stderr_tail"])

    def test_rate_limit_is_retried_then_succeeds(self):
        matches = [{"index": 1, "top3": [{"shot_id": "S1", "score": 88}]}]
        result, record, call = self._match(
            [(1, "HTTP 429 Too Many Requests", None), (0, "", json.dumps(matches))])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)
        self.assertNotIn("semantic_match_error", result)
        self.assertEqual(result["semantic_matches"], matches)
        self.assertEqual((record["error"], record["attempts"], record["stderr_tail"]), (None, 2, ""))


class TemporaryMatchBlockTests(unittest.TestCase):
    def test_failed_ai_match_blocks_before_rule_fallback(self):
        analysis = {"shots": [{"shot_id": "S1", "status": "ready_for_matching"}],
                    "vision_analysis": {"errors": []}}

        def failed_match(_manifest, current, _report_dir):
            current["semantic_match_error"] = "match call codex exit 2: boom"
            current["semantic_matches"] = []
            return current

        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(cli, "_run_shot_analysis", return_value=analysis), \
                mock.patch.object(vision_analyzer, "match_with_codex", side_effect=failed_match), \
                mock.patch.object(cli.shot_analyzer, "match_manifest") as rule_match:
            manifest, blocker = cli._temporary_match({"segments": []}, Path(raw) / "m.json", Path(raw))
        rule_match.assert_not_called()
        self.assertEqual(blocker["status"], "SHOT_MATCH_BLOCKED")
        self.assertIn("VISION_CALL_FAILED", blocker["message"])
        self.assertIn("codex exit 2", blocker["error"])


if __name__ == "__main__":
    unittest.main()
