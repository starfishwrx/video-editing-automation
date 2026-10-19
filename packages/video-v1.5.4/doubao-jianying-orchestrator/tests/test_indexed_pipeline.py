"""Offline index -> observation -> contextual plan coordinator contracts.

All media, model answers and GPU work are synthetic; passing is not a receipt
that any real video meets business requirements.
"""
from __future__ import annotations

import copy
import argparse
import contextlib
import io
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import indexed_pipeline as pipeline
from orchestrator import cli
from orchestrator.engine import OrchestrationEngine


class IndexedEntryGuardTests(unittest.TestCase):
    def test_old_recover_never_invokes_legacy_for_explicit_indexed_strategy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"pipeline_strategy": "indexed_contextual", "product": "sku",
                                            "material_sources": [str(source)]}), encoding="utf-8")
            for stage in ("semantic_match", "draft_write"):
                with self.subTest(stage=stage), mock.patch.object(cli, "_temporary_match") as legacy, \
                        mock.patch.object(cli, "OrchestrationEngine") as engine, contextlib.redirect_stdout(io.StringIO()):
                    report = root / stage
                    code = cli.cmd_recover(argparse.Namespace(input=str(manifest), report_dir=str(report), stage=stage))
                    self.assertEqual(code, 1)
                    result = json.loads((report / "last_result.json").read_text(encoding="utf-8-sig"))
                    self.assertEqual(result["status"], "INDEXED_RECOVERY_REQUIRES_BUILD")
                    legacy.assert_not_called()
                    engine.assert_not_called()

    def test_direct_engine_requires_contextual_contract_for_indexed_strategy(self):
        engine = object.__new__(OrchestrationEngine)
        engine.m = {"pipeline_strategy": "indexed_contextual"}
        self.assertEqual(engine._run_build()["status"], "INDEXED_PLAN_REQUIRED")


class OfflineRetriever:
    def __init__(self, store, models, *, product_id, **kwargs):
        self.store, self.product_id = store, product_id
        self.queries = []

    def index_frames(self, frames):
        return {"frames": len(frames), "encoded": 0, "cache_hits": len(frames), "model": "offline-fixture"}

    def retrieve(self, query, allowed_material_ids, topk=8):
        self.queries.append((query, topk))
        allowed = set(self.store.scoped_ids(self.product_id, allowed_material_ids))
        rows = self.store.sources(allowed)
        ids = list(dict.fromkeys(row["material_id"] for row in rows))
        return [{"material_id": mid, "score": 1 - index / 100} for index, mid in enumerate(ids[:topk])]


class IndexedPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls, self.decodes, self.retrievers = [], [], []
        self.video = self.root / "source_00.mp4"
        self.video.write_bytes(b"synthetic frozen video")
        audio = self.root / "audio.ogg"
        audio.write_bytes(b"synthetic frozen audio")
        self.manifest = {"product_id": "sku-fixture", "pipeline_strategy": "indexed_contextual",
                         "task_id": "offline-fixture", "delivery_mode": "formal", "tail_pad_s": 0,
                         "material_sources": [str(self.video)], "fps": 30,
                         "material_index_dir": str(self.root / "index"),
                         "vision_model_cache_dir": str(self.root / "weights"),
                         "indexed_required_requirements": ["wipe-start-to-clean-result"],
                         "segments": [{"text": "完整擦拭到干净", "audio": str(audio), "audio_duration_us": 2_000_000}]}
        self.groups = [{"id": "g1", "members": [1], "query": "完整擦拭到干净", "require_complete_action": True,
                        "required_requirements": ["wipe-start-to-clean-result"]}]
        self.probe = mock.patch.object(pipeline.shot_analyzer, "probe_duration", return_value=4.0)
        self.probe_mock = self.probe.start()
        self.addCleanup(self.probe.stop)
        self.decode = mock.patch.object(pipeline.shot_analyzer, "extract_frame", side_effect=self.extract)
        self.decode.start()
        self.addCleanup(self.decode.stop)
        self.render = mock.patch.object(pipeline.indexed_planner, "_render", side_effect=self.render_slice)
        self.render.start()
        self.addCleanup(self.render.stop)

    def factory(self, store, models, **kwargs):
        retriever = OfflineRetriever(store, models, **kwargs)
        self.retrievers.append(retriever)
        return retriever

    def extract(self, source, stamp, output, **kwargs):
        self.decodes.append((str(source), stamp))
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (48, 80), (int(stamp * 50) % 256, 50, 100)).save(output)
        return True

    def render_slice(self, rows, output, ffmpeg):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_bytes(json.dumps(rows, sort_keys=True).encode())
        return Path(output)

    def model(self, request):
        self.calls.append(copy.deepcopy(request))
        stage, payload = request["stage"], request["payload"]
        if stage == "group":
            return {"groups": copy.deepcopy(self.groups)}
        if stage == "material_observation":
            return {"description": "Synthetic fixture: full wipe with clean outcome.", "usable": True,
                    "verified_requirements": payload["required_requirements"],
                    "action_segments": [{"start_us": 0, "end_us": 4_000_000, "action": "wipe"}]}
        if stage in {"plan", "repair"}:
            return {"candidate_id": payload["candidates"][0]["candidate_id"], "source_start_us": 0,
                    "source_end_us": 4_000_000, "reason": "Synthetic fixture only."}
        if stage == "observe":
            return {"status": "complete", "action_complete": True, "visible_at_speed": True,
                    "observation": "Synthetic observation only.", "issues": [],
                    "observations": [{"index": member, "status": "supported", "observation": "Synthetic relation."}
                                     for member in payload["members"]]}
        raise AssertionError(stage)

    def run_pipeline(self, *, model=None, report="report"):
        return pipeline.prepare(self.manifest, self.root, self.root / report,
                                model=model or self.model, retriever_factory=self.factory)

    def test_cold_normal_entry_builds_contextual_manifest_and_hot_repeat_understands_zero(self):
        cold = self.run_pipeline()
        self.assertTrue(cold["ok"], cold.get("issues"))
        self.assertEqual(cold["index_stats"]["material_understanding_calls"], 1)
        self.assertTrue(cold["prepared_manifest"]["contextual_plan"])
        self.assertFalse(cold["formal_delivery_allowed"])
        count = len(self.calls)
        before_decodes = len(self.decodes)
        hot = self.run_pipeline()
        self.assertTrue(hot["ok"], hot.get("issues"))
        self.assertEqual(hot["index_stats"]["material_understanding_calls"], 0)
        self.assertFalse(any(c["stage"] == "material_observation" for c in self.calls[count:]))
        self.assertEqual(len(self.decodes), before_decodes)
        self.assertEqual(hot["index_stats"]["index"]["hash_cache_hits"], 1)

    def test_selection_policy_change_does_not_repeat_material_observation(self):
        self.assertTrue(self.run_pipeline()["ok"])
        before = sum(c["stage"] == "material_observation" for c in self.calls)
        self.manifest["selection_policy"] = {"prefer_continuous_action": True}
        changed = self.run_pipeline(report="other-report")
        self.assertTrue(changed["ok"], changed.get("issues"))
        self.assertEqual(sum(c["stage"] == "material_observation" for c in self.calls), before)

    def test_new_requirement_only_rechecks_selected_material(self):
        self.assertTrue(self.run_pipeline()["ok"])
        self.groups[0]["required_requirements"].append("show-clean-hand-after-wipe")
        self.manifest["indexed_required_requirements"].append("show-clean-hand-after-wipe")
        changed = self.run_pipeline(report="new-requirement")
        self.assertTrue(changed["ok"], changed.get("issues"))
        self.assertEqual(changed["index_stats"]["material_understanding_calls"], 1)
        latest = [c for c in self.calls if c["stage"] == "material_observation"][-1]
        self.assertIn("show-clean-hand-after-wipe", latest["payload"]["required_requirements"])

    def test_negative_observation_is_reused_on_hot_run(self):
        def negative(request):
            if request["stage"] == "material_observation":
                self.calls.append(copy.deepcopy(request))
                return {"description": "Only packaging; no wiping action.", "usable": False,
                        "verified_requirements": [], "uncertain_requirements": request["payload"]["required_requirements"],
                        "action_segments": []}
            return self.model(request)
        self.assertFalse(self.run_pipeline(model=negative)["ok"])
        before = sum(c["stage"] == "material_observation" for c in self.calls)
        repeated = self.run_pipeline(model=negative)
        self.assertFalse(repeated["ok"])
        self.assertEqual(repeated["index_stats"]["material_understanding_calls"], 0)
        self.assertEqual(sum(c["stage"] == "material_observation" for c in self.calls), before)

    def test_legacy_partial_window_does_not_replace_full_material_duration(self):
        image = self.root / "legacy.jpg"
        Image.new("RGB", (48, 80), "blue").save(image)
        analysis = self.root / "old-analysis.json"
        analysis.write_text(json.dumps({"shots": [{"video": str(self.video), "source_start": 0,
            "source_end": 1, "material_duration_us": 4_000_000, "material_duration_confidence": "HIGH",
            "frame_path": str(image), "frame_time": .5, "description": "lead-in only"}]}), encoding="utf-8")
        self.manifest["shot_analysis_file"] = str(analysis)
        result = self.run_pipeline()
        request = next(c for c in self.calls if c["stage"] == "material_observation")
        self.assertEqual(request["payload"]["duration_us"], 4_000_000)
        self.assertTrue(result["ok"], result.get("issues"))

    def test_empty_first_eight_expands_to_sixteen_and_leaves_unused_materials_pending(self):
        videos = [self.video]
        for index in range(1, 18):
            path = self.root / f"source_{index:02d}.mp4"
            path.write_bytes(f"synthetic source {index}".encode())
            videos.append(path)
        self.manifest["material_sources"] = [str(path) for path in videos]
        selected = set()
        def sparse(request):
            if request["stage"] == "material_observation":
                self.calls.append(copy.deepcopy(request))
                selected.add(request["images"][0]["material_id"])
                # The 9th distinct candidate is the first usable one.
                usable = len(selected) >= 9
                return {"description": "Synthetic complete wipe." if usable else "Packaging only.", "usable": usable,
                        "verified_requirements": request["payload"]["required_requirements"] if usable else [],
                        "action_segments": [{"start_us": 0, "end_us": 4_000_000, "action": "wipe"}] if usable else []}
            return self.model(request)
        result = self.run_pipeline(model=sparse)
        self.assertTrue(result["ok"], result.get("issues"))
        self.assertEqual([topk for _, topk in self.retrievers[-1].queries], [8, 16])
        self.assertEqual(result["index_stats"]["material_understanding_calls"], 16)
        self.assertEqual(len(selected), 16)

    def test_nonempty_but_semantically_wrong_first_eight_also_expand(self):
        videos = [self.video]
        for index in range(1, 10):
            path = self.root / f"source_{index:02d}.mp4"
            path.write_bytes(f"synthetic source {index}".encode())
            videos.append(path)
        good_id = hashlib.sha256(videos[8].read_bytes()).hexdigest()
        self.manifest["material_sources"] = [str(path) for path in videos]
        self.groups[0]["required_requirements"] = []
        def semantic_model(request):
            if request["stage"] == "material_observation":
                self.calls.append(copy.deepcopy(request))
                good = request["images"][0]["material_id"] == good_id
                return {"description": "GOOD: complete wiping action." if good else "Packaging display only.",
                        "usable": True, "verified_requirements": [],
                        "action_segments": [{"start_us": 0, "end_us": 4_000_000, "action": "wipe"}] if good else []}
            if request["stage"] in {"plan", "repair"}:
                self.calls.append(copy.deepcopy(request))
                good = next((candidate for candidate in request["payload"]["candidates"]
                             if candidate["description"].startswith("GOOD:")), None)
                if not good:
                    return {"gap": "Only packaging in shortlist; no complete wiping action."}
                return {"candidate_id": good["candidate_id"], "source_start_us": 0,
                        "source_end_us": 4_000_000, "reason": "Synthetic fixture only."}
            return self.model(request)
        result = self.run_pipeline(model=semantic_model)
        self.assertTrue(result["ok"], result.get("issues"))
        self.assertIn(16, [topk for _, topk in self.retrievers[-1].queries])


if __name__ == "__main__":
    unittest.main()
