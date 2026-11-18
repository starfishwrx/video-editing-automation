"""Exercise product preparation, HTTP validation, persisted attempts and task handoff together."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from unittest import mock

from test_material_preparation_recovery import BAD_ID, GOOD, _ProductBase, material_cache, shot_analyzer, vision
from orchestrator.vision import vision_client


class MaterialFormatTests(_ProductBase):
    def setUp(self):
        super().setUp()
        os.environ.update(JY_VISION_TRANSPORT="qwen", JY_QWEN_API_KEY="test", JY_QWEN_MODEL="test",
                          JY_QWEN_BASE_URL="http://example.invalid", JY_VISION_SHEET_TILES="2",
                          JY_VISION_TEXT_PASS="1", JY_MATERIAL_EVIDENCE_DB=str(self.root / "evidence.db"))
        self.stack.enter_context(mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw))
        self.stack.enter_context(mock.patch.object(vision, "_sample_frames", side_effect=self.frames))
        self.stack.enter_context(mock.patch.object(vision, "_orientation", return_value="portrait"))
        self.stack.enter_context(mock.patch.object(vision, "_write_sheet", side_effect=lambda items, path, **kw:
                                                  bool(Path(path).write_bytes(b"image"))))
        self.stack.enter_context(mock.patch.object(vision_client.random, "uniform", return_value=0))
        self.bad_phase = "vision"
        self.bad_status = 200
        self.bad_content = "{}"
        self.good_text = []
        self.fail_all = False
        self.calls = []
        self.stack.enter_context(mock.patch.object(vision_client, "_http", side_effect=self.reply))

    def frames(self, shot, directory, ffmpeg):
        picture = directory / "sample.jpg"
        picture.write_bytes(Path(shot["video"]).read_bytes())
        return [{"path": str(picture), "timestamp": t, "suffix": str(i)} for i, t in enumerate((0.5, 1.5))]

    async def reply(self, url, api_key, body, timeout, validator):
        original = body["messages"][0]["content"]
        prompt = original[0]["text"]
        phase = "text" if "逐字抄录" in prompt else "vision"
        sid = BAD_ID if f"shot_id={BAD_ID}" in prompt else "source_001_shot_001"
        self.calls.append((sid, phase, body))
        if phase == self.bad_phase and (self.fail_all or sid == BAD_ID):
            content, status = self.bad_content, self.bad_status
        else:
            status = 200
            value = ({"shots": [{"shot_id": sid, **GOOD}]} if phase == "vision"
                     else {"shot_id": sid, "readable_claims": self.good_text, "pointed_text": self.good_text})
            content = json.dumps(value, ensure_ascii=False)
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
        return vision_client._parse_response(json.dumps(payload).encode(), status, {"x-request-id": "test"}, validator)

    def product(self):
        return material_cache.analyze_product(self.manifest, self.root, self.root / "product")

    def assert_excluded(self, result):
        self.assertTrue(result["material_cache"]["ready"])
        self.assertEqual(vision.logged_pool_exclusions(result), {BAD_ID})
        row = next(s for s in result["shots"] if s["shot_id"] == BAD_ID)
        self.assertEqual(row["status"], "pending_analysis")
        self.assertFalse(vision._reusable_visual_observation(row))
        self.assertEqual([s["shot_id"] for s in result["shots"] if not vision.needs_vision(s)],
                         ["source_001_shot_001"])
        return row

    def test_visual_format_failure_keeps_other_ocr_and_reuses_bound_exclusion(self):
        result = self.product()
        self.assert_excluded(result)
        attempts = [body for sid, phase, body in self.calls if sid == BAD_ID and phase == "vision"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["response_format"], {"type": "json_object"})
        self.assertEqual(attempts[1]["messages"][0], attempts[0]["messages"][0])
        self.assertIn("shots", attempts[1]["messages"][1]["content"])
        self.assertIn("顶层为空", attempts[1]["messages"][1]["content"])
        self.assertEqual(sum(phase == "text" for _, phase, _ in self.calls), 2)
        self.calls.clear()
        self.assert_excluded(self.product())
        self.assertEqual(self.calls, [])

    def test_ocr_failure_preserves_basic_facts_without_certifying_package_text(self):
        self.bad_phase = "text"
        self.bad_content = '{"shot_id":'
        result = self.product()
        self.assertEqual(self.assert_excluded(result)["text_pass"], "failed")
        self.assertEqual(result["text_pass"]["failed"], 1)
        retry = next(body for sid, phase, body in self.calls
                     if sid == BAD_ID and phase == "text" and len(body["messages"]) == 2)
        self.assertIn("列", retry["messages"][1]["content"])
        sha = next(x["source_sha256"] for x in result["shots"] if x["shot_id"] == BAD_ID)
        with closing(sqlite3.connect(os.environ["JY_MATERIAL_EVIDENCE_DB"])) as db:
            self.assertEqual(db.execute("SELECT grade,json_extract(payload,'$.text_pass') FROM observations "
                "WHERE sha=? AND grade IN ('visual','label')", (sha,)).fetchall(), [("visual", "failed")])
        self.calls.clear()
        self.assert_excluded(self.product())
        self.assertEqual(self.calls, [])

    def test_systemic_format_failures_block_the_product(self):
        self.fail_all = True
        self.assertFalse(self.product()["material_cache"]["ready"])

    def test_disabled_recovery_blocks_even_one_format_failure(self):
        os.environ["JY_VISION_REASK_LIMIT"] = "0"
        result = self.product()
        self.assertFalse(result["material_cache"]["ready"])
        self.assertFalse(vision.logged_pool_exclusions(result))

    def test_unknown_shot_id_cannot_be_used_as_the_only_selling_point_evidence(self):
        from orchestrator.selection import ai_editor
        self.bad_content = '{"shots":[{"shot_id":"private-output"}]}'
        result = self.product()
        self.assert_excluded(result)
        retry = next(body for sid, phase, body in self.calls if sid == BAD_ID and len(body["messages"]) == 2)
        self.assertIn("shot_id", retry["messages"][1]["content"])
        self.assertNotIn("private-output", retry["messages"][1]["content"])
        claim = "包装明确标有120克"
        self.manifest["segments"] = [{"text": claim, "audio_duration_us": 2000000}]
        sentence = {"index": 1, "text": claim, "kind": "selling_point", "selling_points": [{"id": "p1", "text": claim}],
                    "candidate_ids": ["source_001_shot_001"], "scene_ids": []}
        coverage = {"feasible": False, "exhausted": True, "reason": "NO_MATCHED_EVIDENCE"}
        with mock.patch.object(ai_editor, "_inventory", return_value=[sentence]) as inventory, \
             mock.patch.object(ai_editor, "_review_pool", return_value=([], coverage)) as review:
            prepared = ai_editor.prepare(self.manifest, result, self.root / "selection")
        self.assertEqual([s["shot_id"] for s in inventory.call_args.args[1]], ["source_001_shot_001"])
        self.assertEqual(list(review.call_args.args[2]), ["source_001_shot_001"])
        self.assertFalse(prepared["ai_editor_plan"]["ok"])
        self.assertFalse(ai_editor.match_manifest(self.manifest, prepared)["ok"])

    def test_http_400_stops_material_without_exclusion(self):
        from orchestrator.application import cli
        from orchestrator.selection import ai_editor
        self.bad_status = 400
        self.bad_phase = "text"
        result = self.product()
        self.assertFalse(result["material_cache"]["ready"])
        self.assertFalse(vision.logged_pool_exclusions(result))
        self.assertEqual(sum(sid == BAD_ID and phase == "text" for sid, phase, _ in self.calls), 1)
        with mock.patch.object(cli, "_run_shot_analysis", return_value=result), \
             mock.patch.object(ai_editor, "prepare") as prepare:
            _, blocker = cli._match_temporary_input(self.manifest, self.root / "manifest.json", self.root)
        self.assertEqual(blocker["status"], "SHOT_MATCH_BLOCKED")
        prepare.assert_not_called()

    def test_source_replaced_with_same_stat_reopens_exclusion_and_resamples_pixels(self):
        self.assert_excluded(self.product())
        path = self.videos[1]
        before = path.stat()
        path.write_bytes(b"x" * before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.bad_phase = None
        self.calls.clear()
        result = self.product()
        self.assertTrue(result["material_cache"]["ready"])
        self.assertFalse(vision.logged_pool_exclusions(result))
        self.assertIn((BAD_ID, "vision"), [(sid, phase) for sid, phase, _ in self.calls])
        row = next(row for row in result["shots"] if row["shot_id"] == BAD_ID)
        self.assertEqual(Path(row["vision_frames"][0]["path"]).read_bytes(), path.read_bytes())

    def test_recovered_ocr_is_reusable_from_sql_after_local_cache_changes(self):
        from orchestrator.vision import evidence_store
        self.bad_phase = "text"
        self.assert_excluded(self.product())
        self.bad_phase = None
        self.good_text = ["120克"]
        os.environ["JY_OCR_MODEL"] = "another-ocr"
        result = self.product()
        row = next(row for row in result["shots"] if row["shot_id"] == BAD_ID)
        self.assertEqual(row["text_pass"], "hires")
        self.assertTrue(evidence_store.portable_visual(self.manifest, row))
        self.assertEqual(evidence_store.portable_text(self.manifest, row)["pointed_agreed"], self.good_text)
        os.environ["JY_VISION_CACHE_DIR"] = str(self.root / "other-cache")
        self.calls.clear()
        rebuilt = material_cache.analyze_product(self.manifest, self.root, self.root / "other-product")
        self.assertEqual(next(s for s in rebuilt["shots"] if s["shot_id"] == BAD_ID)["text_pass"], "hires")
        self.assertFalse(any(sid == BAD_ID for sid, _, _ in self.calls))

    def test_unknown_previous_text_cannot_confirm_another_shot(self):
        rows = [{"text_pass": "hires", "text_agreed": ["120克"]},
                {"excluded_from_pool": True, "text_agreed": ["120克"]}]
        vision.apply_cross_shot_text(rows)
        self.assertEqual(rows[0]["readable_claims"], [])

    def test_source_change_with_sql_disabled_does_not_reuse_old_ocr_pixels(self):
        os.environ["JY_MATERIAL_EVIDENCE_DB"] = "off"
        self.bad_phase = "text"
        self.test_source_replaced_with_same_stat_reopens_exclusion_and_resamples_pixels()

    def test_unsigned_historical_cache_reobserves_before_certifying_source(self):
        os.environ["JY_MATERIAL_EVIDENCE_DB"] = "off"
        self.bad_phase = None
        self.assertTrue(self.product()["material_cache"]["ready"])
        path = vision._vision_cache_path(self.root / "product")
        cached = vision._load_vision_cache(path)
        for entry in cached.values():
            entry.pop("source_sha256", None)
        path.write_text(json.dumps(cached), encoding="utf-8")
        source = self.videos[1]
        before = source.stat()
        source.write_bytes(b"x" * before.st_size)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.calls.clear()
        rebuilt = material_cache.analyze_product(self.manifest, self.root, self.root / "new-product")
        self.assertTrue(rebuilt["material_cache"]["ready"])
        self.assertIn((BAD_ID, "vision"), [(sid, phase) for sid, phase, _ in self.calls])
