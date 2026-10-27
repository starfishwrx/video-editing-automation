from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import cli, demo_actions, vision_analyzer  # noqa: E402

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover - Pillow ships with the Windows runtime
    Image = None

SHOT_ID = "source_001_shot_001"


def _first_pass(**overrides) -> str:
    row = {
        "shot_id": SHOT_ID,
        "description": "单手从挂钩上的包装抽出一张，盒口留出下一张的头",
        "visual_tags": ["挂钩", "单手抽取"], "evidence_tags": ["单手抽出一张"],
        "action_complete": True, "head_waste": 0.0, "tail_waste": 0.1,
        "evidence_time": 7.4, "evidence_intervals": [{"start": 6.0, "end": 8.0}],
        "action_segments": [{"start": 6.0, "end": 7.8, "action": "单手抽出一张",
                             "result": "盒口留出下一张的头"}],
        "text_tiles": [3], "confidence": 0.8, "action": "抽出", "object": "湿厕纸",
        "result": "留出下一张的头", "role": "usage_demo", "evidence_strength": "B",
        "action_phase": "perform", "setup_id": "wall_hook", "has_subject": True,
        "readable_claims": ["80抽", "0酒精"], "pointing_to_text": False,
        "pointing_action": "", "gesture": "",
    }
    row.update(overrides)
    return json.dumps({"shots": [row]}, ensure_ascii=False)


def _text(claims: list[str]) -> tuple[bool, str]:
    return True, json.dumps({"shot_id": SHOT_ID, "readable_claims": claims,
                             "pointed_text": []}, ensure_ascii=False)


@unittest.skipIf(Image is None, "Pillow is required for contact sheets")
class ResumeIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "IMG_0001.MOV"
        self.video.write_bytes(b"source")
        self.frames = []
        for i, t in enumerate((1.0, 4.0, 7.4), 1):
            path = self.root / f"f{i}.jpg"
            Image.new("RGB", (108, 192), (40 * i, 80, 120)).save(path)
            self.frames.append({"path": str(path), "timestamp": t, "suffix": f"t{i}"})
        self.manifest = {"category": "纸品", "product": "纯水湿厕纸", "product_id": "p41"}
        self.report = self.root / "task" / "report"
        self.env = mock.patch.dict(os.environ, {"JY_VISION_CACHE_DIR": str(self.root / "cache"),
                                                "JY_VISION_TEXT_PASS": "1",
                                                "JY_VISION_TEXT_MIN_SHOTS": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _shot(self) -> dict:
        return {"shot_id": SHOT_ID, "video": str(self.video), "source_start": 0.0,
                "source_end": 9.4, "frame_exists": True, "status": "pending_analysis",
                "description": ""}

    def _run(self, analysis: dict, responses: list, manifest: dict | None = None):
        prompts: list[str] = []

        def fake(sheet, prompt, *_a, on_attempt=None, **_kw):
            prompts.append(prompt)
            if on_attempt is not None:
                on_attempt(1, 1, "")
            if not responses:
                raise TimeoutError("build killed mid text pass")
            return responses.pop(0)

        with mock.patch.object(vision_analyzer, "_sample_frames", return_value=self.frames), \
                mock.patch.object(vision_analyzer, "_run_vision_with_retry", side_effect=fake):
            result = vision_analyzer.enrich_with_vision(
                manifest or self.manifest, analysis, self.report, profile="volc")
        return result, prompts

    def _killed_during_text(self) -> dict:
        """First run dies inside the text pass; returns the candidates file it left."""
        with self.assertRaises(TimeoutError):
            self._run({"shots": [self._shot()]}, [(True, _first_pass())])
        return json.loads((self.report / "shot_candidates.json").read_text(encoding="utf-8"))

    def _cache(self) -> dict:
        path = self.root / "cache" / "material_vision.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def test_partial_file_holds_no_unverified_text_and_is_not_ok(self):
        partial = self._killed_during_text()
        row = partial["shots"][0]
        self.assertEqual(row["status"], "ready_for_matching")
        self.assertEqual(row["text_pass"], "pending")
        self.assertEqual(row["readable_claims"], [])
        self.assertTrue(row["text_request_frames"])
        self.assertFalse(partial["ok"])
        self.assertTrue(vision_analyzer.needs_vision(row))
        cached = next(iter(self._cache().values()))
        self.assertEqual(cached["text_pass"], "pending")
        self.assertTrue(cached["action_complete"])
        self.assertFalse(partial["ok"])
        progress = json.loads((self.report / "vision_progress.json").read_text(encoding="utf-8"))
        self.assertEqual(progress["phase"], "text_read_1")
        self.assertEqual(progress["shot_ids"], [SHOT_ID])

    def test_resume_runs_only_the_missing_text_pass(self):
        partial = self._killed_during_text()
        result, prompts = self._run(partial, [_text(["80抽", "EDI纯水"]), _text(["80抽"])])
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("逐字抄录" in p for p in prompts))
        row = result["shots"][0]
        self.assertEqual(row["text_pass"], "hires")
        self.assertEqual(row["readable_claims"], ["80抽"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["vision_analysis"]["text_resumed"], 1)
        self.assertEqual(next(iter(self._cache().values()))["readable_claims"], ["80抽"])
        progress = json.loads((self.report / "vision_progress.json").read_text(encoding="utf-8"))
        self.assertEqual(progress["phase"], "done")

    def test_resume_with_failed_text_keeps_action_but_no_text(self):
        partial = self._killed_during_text()
        result, prompts = self._run(partial, [(False, "timed out")])
        row = result["shots"][0]
        self.assertEqual(row["status"], "ready_for_matching")
        self.assertEqual(row["text_pass"], "failed")
        self.assertEqual(row["readable_claims"], [])
        self.assertEqual(next(iter(self._cache().values()))["text_pass"], "failed")
        self.assertFalse(result["ok"])

    def test_resume_without_frames_prepares_only_text_images(self):
        partial = self._killed_during_text()
        row = partial["shots"][0]
        row["text_request_frames"] = [{"path": str(self.root / "gone.jpg"), "timestamp": 7.4}]
        row["vision_frames"] = []
        row["evidence_intervals"] = [{"start": 6.0, "end": 8.0}]
        result, prompts = self._run(partial, [_text(["80抽"]), _text(["80抽"])])
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("逐字抄录" in prompt for prompt in prompts))
        self.assertEqual(result["shots"][0]["status"], "ready_for_matching")

    def test_finished_local_rows_with_same_signature_are_kept_and_seeded(self):
        done, _ = self._run({"shots": [self._shot()]},
                            [(True, _first_pass()), _text(["80抽"]), _text(["80抽"])])
        (self.root / "cache" / "material_vision.json").unlink()
        result, prompts = self._run(json.loads(json.dumps(done)), [])
        self.assertEqual(prompts, [])
        self.assertTrue(result["ok"])
        self.assertEqual(result["shots"][0]["readable_claims"], ["80抽"])
        self.assertEqual(len(self._cache()), 1)

    def test_changed_rules_invalidate_local_ready_rows(self):
        done, _ = self._run({"shots": [self._shot()]},
                            [(True, _first_pass()), _text(["80抽"]), _text(["80抽"])])
        (self.root / "cache" / "material_vision.json").unlink()
        old_key = done["shots"][0]["analysis_cache_key"]
        with mock.patch.object(vision_analyzer, "_analysis_rules", return_value=["新增规则"]), \
                mock.patch.object(vision_analyzer, "_keyed_rules", return_value=["新增规则"]):
            result, prompts = self._run(json.loads(json.dumps(done)),
                                        [(True, _first_pass(text_tiles=[], readable_claims=[]))])
        self.assertEqual(len(prompts), 1)
        self.assertIn("新增规则", prompts[0])
        self.assertEqual(result["vision_analysis"]["stale_local"], 1)
        self.assertNotIn(old_key, self._cache())
        self.assertNotEqual(result["shots"][0]["analysis_cache_key"], old_key)

    def test_changed_product_invalidates_local_ready_rows(self):
        done, _ = self._run({"shots": [self._shot()]},
                            [(True, _first_pass()), _text(["80抽"]), _text(["80抽"])])
        (self.root / "cache" / "material_vision.json").unlink()
        other = {**self.manifest, "product_id": "p99"}
        result, prompts = self._run(json.loads(json.dumps(done)),
                                    [(True, _first_pass(text_tiles=[], readable_claims=[]))],
                                    manifest=other)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result["vision_analysis"]["stale_local"], 1)

    def test_legacy_row_without_signature_is_reanalyzed(self):
        legacy = {**self._shot(), "status": "ready_for_matching", "description": "旧观察",
                  "action_complete": True, "text_pass": "hires", "readable_claims": ["旧字"]}
        result, prompts = self._run({"shots": [legacy]},
                                    [(True, _first_pass(text_tiles=[], readable_claims=[]))])
        self.assertEqual(len(prompts), 1)
        self.assertNotEqual(result["shots"][0]["description"], "旧观察")


    def test_text_pass_runs_concurrently_and_every_shot_lands(self):
        shots = []
        for n in range(1, 7):
            shot = self._shot()
            shot.update({"shot_id": f"s{n}", "source_start": n * 10.0, "source_end": n * 10.0 + 9.4})
            shots.append(shot)
        first = json.dumps({"shots": [json.loads(_first_pass())["shots"][0] | {"shot_id": s["shot_id"]}
                                      for s in shots]}, ensure_ascii=False)
        import threading
        import time as _time
        active, peak, lock = [0], [0], threading.Lock()

        def fake(sheet, prompt, *_a, on_attempt=None, **_kw):
            if "逐字抄录" not in prompt:
                return True, first
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.05)
            with lock:
                active[0] -= 1
            shot_id = prompt.split("shot_id=")[1].split("（")[0]
            return True, json.dumps({"shot_id": shot_id, "readable_claims": ["80抽"],
                                     "pointed_text": []}, ensure_ascii=False)

        with mock.patch.dict(os.environ, {"JY_VISION_TEXT_WORKERS": "3",
                                          "JY_VISION_SHEET_TILES": "40"}), \
                mock.patch.object(vision_analyzer, "_sample_frames", return_value=self.frames), \
                mock.patch.object(vision_analyzer, "_run_vision_with_retry", side_effect=fake):
            result = vision_analyzer.enrich_with_vision(self.manifest, {"shots": shots},
                                                        self.report, profile="volc")
        self.assertGreater(peak[0], 1)
        self.assertLessEqual(peak[0], 3)
        self.assertTrue(all(row["text_pass"] == "hires" for row in result["shots"]))
        self.assertEqual(len(self._cache()), 6)


class SharedCacheMergeTest(unittest.TestCase):
    def test_parallel_writers_keep_each_others_entries(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "material_vision.json"
            first, second = {"a": {"status": "ready_for_matching"}}, {"b": {"status": "ready_for_matching"}}
            vision_analyzer._save_vision_cache(path, first)
            vision_analyzer._save_vision_cache(path, second)
            disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(disk), {"a", "b"})
            self.assertIn("a", second)


class UnverifiedTextEvidenceTest(unittest.TestCase):
    contract = {"kind": "functional", "product": "湿厕纸", "counts": ["80"],
                "sheet_counts": ["80"], "topics": [{"id": "count", "match": "count"}]}

    def _shot(self, text_pass: str | None) -> dict:
        shot = {"description": "包装正面印着80抽", "readable_claims": ["80抽"]}
        if text_pass:
            shot["text_pass"] = text_pass
        return shot

    def test_count_needs_verified_text(self):
        self.assertEqual(demo_actions.shot_fit(self._shot("hires"), self.contract), 2)
        self.assertLess(demo_actions.shot_fit(self._shot("pending"), self.contract), 2)
        self.assertLess(demo_actions.shot_fit(self._shot("failed"), self.contract), 2)

    def test_packaging_claim_needs_verified_text(self):
        key, variants = next(iter(demo_actions._PACKAGING_CLAIMS.items()))
        shot = {"readable_claims": [variants[0]], "pointing_to_text": True,
                "text_pass": "pending"}
        verdict = demo_actions.packaging_claim_direct(shot, variants[0])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["reason"], "PACKAGING_TEXT_UNVERIFIED")


class CliReuseTest(unittest.TestCase):
    def test_all_ready_candidates_still_go_through_enrich(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidates = {"ok": True, "shots": [{"shot_id": SHOT_ID,
                                                 "status": "ready_for_matching",
                                                 "text_pass": "pending"}]}
            (root / "cand.json").write_text(json.dumps(candidates), encoding="utf-8")
            manifest = {"shot_analysis_file": str(root / "cand.json")}
            with mock.patch.object(cli, "environment_report", return_value={}), \
                    mock.patch.object(vision_analyzer, "enrich_with_vision",
                                      return_value={"ok": False}) as enrich:
                result = cli._run_shot_analysis(manifest, root / "manifest.json", root / "report")
            enrich.assert_called_once()
            self.assertEqual(result, {"ok": False})

    def test_vision_disabled_returns_file_as_is(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "cand.json").write_text(json.dumps({"shots": []}), encoding="utf-8")
            manifest = {"shot_analysis_file": str(root / "cand.json"), "vision_analysis": False}
            with mock.patch.object(vision_analyzer, "enrich_with_vision") as enrich:
                result = cli._run_shot_analysis(manifest, root / "manifest.json", root / "report")
            enrich.assert_not_called()
            self.assertEqual(result, {"shots": []})


class NarrationProbeTest(unittest.TestCase):
    def tearDown(self):
        cli._PROBE_CACHE.clear()

    def test_pre_synthesis_measures_audio_like_the_engine(self):
        cli._PROBE_CACHE.clear()
        engine_ruler = {"duration_us": 1_545_771, "source": "container_duration", "confidence": "LOW"}
        with mock.patch("orchestrator.platform_env.find_ffmpeg",
                        return_value=(Path("ffmpeg"), Path("ffprobe"))), \
                mock.patch.object(cli.timing_contract, "probe_material_duration_us",
                                  return_value=engine_ruler) as probe:
            measured = cli._audio_probe()("tts.ogg")
        probe.assert_called_once()
        self.assertEqual(round(measured["duration"] * 1_000_000), 1_545_771)


if __name__ == "__main__":
    unittest.main()
