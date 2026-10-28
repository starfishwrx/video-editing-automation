"""New mode through saved-draft QC and the worker's actual report consumer."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import test_ai_editor as fixtures
from orchestrator import ai_editor_contract as contract, ai_editor_qc
from orchestrator import draft_visual_qc as qc, vision_analyzer, visual_qc_contract


class AIEditorQCIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AIEditorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.match = self.fixture.result()
        self.segment = self.match["segments"][0]
        self.report = self.root / "report"
        self.draft = self.root / "draft"
        self.report.mkdir()
        self.draft.mkdir()
        self.write_reports()

    def write_reports(self):
        (self.report / "shot_match_report.json").write_text(json.dumps(self.match), encoding="utf-8")
        (self.report / "shot_candidates.json").write_text(json.dumps({"shots": [self.fixture.shot]}), encoding="utf-8")
        (self.draft / "ai_selection_identity.json").write_text(json.dumps({
            "schema": "chatcut-ai-draft-v1", "selection_mode": self.match.get("selection_mode", "ai_editor"),
            "editorial_rules": contract.rules_identity(self.match["manifest"]), "policy_version": contract.POLICY_VERSION,
            "selection_sha256": contract.digest(self.match["segments"])}), encoding="utf-8")
        info = {"duration": 2000000, "materials": {"videos": [{"id": "v1", "path": str(self.fixture.video)}]},
            "tracks": [{"type": "video", "segments": [{"material_id": "v1",
                "source_timerange": {"start": round(self.segment["source_start"] * 1e6),
                                     "duration": round(self.segment["duration"] * 1e6)},
                "target_timerange": {"start": 0, "duration": 2000000}}]}]}
        (self.draft / "draft_info.json").write_text(json.dumps(info), encoding="utf-8")

    def run_qc(self, version=None):
        model = {"response_version": version or ai_editor_qc.RESPONSE_VERSION, "overall": "PASS", "segments": [
            {"index": 1, "match_level": "DIRECT", "visual_ok": True, "observed": "合成轻薄展示", "reason": "符合本句体验表达", "confidence": .9}]}
        argv = ["qc", "--draft", str(self.draft), "--report-dir", str(self.report), "--output", str(self.root / "qc.json")]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(qc, "_ffmpeg", return_value="synthetic"), \
                mock.patch.object(qc, "_extract", return_value=(True, "")), \
                mock.patch.object(vision_analyzer, "_write_sheet", return_value=True), \
                mock.patch.object(qc, "_call_qc_model", return_value=(True, json.dumps(model))), \
                contextlib.redirect_stdout(io.StringIO()):
            code = qc.main()
        return code, json.loads((self.root / "qc.json").read_text(encoding="utf-8"))

    def test_v4_saved_draft_qc_and_standalone_consumer_both_pass(self):
        code, result = self.run_qc()
        self.assertEqual((code, result["status"]), (0, "PASS"), result)
        self.assertEqual(visual_qc_contract.report_status(result), "PASS")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from tools import production_eval
        self.assertTrue(production_eval.visual_qc_contract.report_matches_draft(result, self.draft))
        self.fixture.video.write_bytes(b"source changed after QC")
        self.assertFalse(production_eval.visual_qc_contract.report_matches_draft(result, self.draft))

    def test_legacy_response_cannot_certify_ai_draft(self):
        code, result = self.run_qc(visual_qc_contract.RESPONSE_VERSION)
        self.assertEqual((code, result["status"]), (2, "UNCERTIFIED"), result)

    def test_actual_draft_cut_cannot_drop_the_result(self):
        info = json.loads((self.draft / "draft_info.json").read_text())
        info["tracks"][0]["segments"][0]["source_timerange"]["duration"] = 100000
        (self.draft / "draft_info.json").write_text(json.dumps(info))
        code, result = self.run_qc()
        self.assertEqual((code, result["status"]), (2, "FAIL"), result)
        self.assertIn("AI_QC_EVIDENCE_CUT_AWAY", result["issues"])

    def test_mixed_preview_requires_sealed_identity_or_explicit_diagnostic_mode(self):
        rule = {"text": "产品展示", "claim_text": "产品展示", "selection_mode": "auto_top1",
                "temporary_shot_id": "rule", "degraded_no_match": True}
        self.match["selection_mode"] = "rule_then_ai"
        self.match["manifest"]["selection_mode"] = "rule_then_ai"
        self.match["segments"].append(rule)
        self.write_reports()
        info_path = self.draft / "draft_info.json"
        info = json.loads(info_path.read_text())
        first = info["tracks"][0]["segments"][0]
        info["tracks"][0]["segments"].append({**first, "target_timerange": {"start": 2000000, "duration": 2000000}})
        info["duration"] = 4000000
        info_path.write_text(json.dumps(info))
        with mock.patch.dict("os.environ", {"JY_POST_QC_BATCH_CLAIMS": "1"}, clear=False):
            code, sealed = self.run_qc()
            self.assertNotIn("MIXED_SELECTION_MODES", sealed["issues"])
            self.assertTrue(sealed.get("ai_final_windows_verified"), sealed)
            self.assertFalse(sealed.get("final_windows_verified"), sealed)
            self.assertEqual(visual_qc_contract.report_status(sealed), "PASS_WITH_DEGRADED")
            self.assertTrue(visual_qc_contract.report_matches_draft(sealed, self.draft))
            (self.draft / "ai_selection_identity.json").unlink()
            _, closed = self.run_qc()
            self.assertEqual(closed["status"], "UNCERTIFIED")
            with mock.patch.dict("os.environ", {"JY_ALLOW_UNSEALED_MIXED_QC": "1"}):
                _, diagnostic = self.run_qc()
        self.assertEqual(diagnostic["status"], "UNCERTIFIED")
        self.assertIn("MIXED_DRAFT_IDENTITY_NOT_SEALED_PREVIEW_ONLY", diagnostic["warnings"])
        self.assertEqual(diagnostic["release_eligibility"], "BLOCKED")

    def test_key_frames_remain_in_chronological_order(self):
        claim = {"ai_segment": self.segment}
        row = {"source_start": self.segment["source_start"], "source_duration": self.segment["duration"]}
        frames = ai_editor_qc.frame_plan(claim, [row])
        self.assertEqual([r[2] for r in frames], sorted(r[2] for r in frames))

    def test_staged_media_is_accepted_only_when_bytes_and_original_identity_match(self):
        staged = self.root / "staged.mp4"
        staged.write_bytes(self.fixture.video.read_bytes())
        expected = contract.source_identity(self.fixture.video)
        self.assertTrue(ai_editor_qc.source_matches_receipt(str(staged), expected))
        staged.write_bytes(b"different staged bytes")
        self.assertFalse(ai_editor_qc.source_matches_receipt(str(staged), expected))
        staged.write_bytes(self.fixture.video.read_bytes())
        self.fixture.video.write_bytes(b"original changed")
        self.assertFalse(ai_editor_qc.source_matches_receipt(str(staged), expected))

    def test_worker_accepts_new_receipts_without_legacy_fields(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import batch_worker
        ok, message = batch_worker.validate_visual_match(self.report)
        self.assertTrue(ok, message)
        self.match["segments"][0]["ai_selection"]["observation"] = ""
        (self.report / "shot_match_report.json").write_text(json.dumps(self.match), encoding="utf-8")
        ok, message = batch_worker.validate_visual_match(self.report)
        self.assertFalse(ok, message)

    def test_worker_validates_both_rule_and_ai_parts_of_mixed_preview(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import batch_worker
        rule_video = self.root / "rule.mp4"
        rule_video.write_bytes(b"rule source")
        rule_shot = {**self.fixture.shot, "shot_id": "rule", "video": str(rule_video),
                     "status": "ready_for_matching", "description": "产品展示"}
        rule = {"text": "产品展示", "claim_text": "产品展示", "selection_mode": "auto_top1",
                "temporary_shot_id": "rule", "video": str(rule_video), "source_start": 0.0,
                "duration": 2.0, "visual_description": "产品展示", "degraded_no_match": True}
        self.match["selection_mode"] = "rule_then_ai"
        self.match["manifest"].update(selection_mode="rule_then_ai", delivery_mode="preview")
        self.match["segments"].append(rule)
        self.match["manifest"]["segments"] = self.match["segments"]
        self.match["accepted_shots"] = [self.segment["temporary_shot_id"], "rule"]
        self.write_reports()
        ai_shot = {**self.fixture.shot, "description": "已复核的产品展示"}
        (self.report / "shot_candidates.json").write_text(json.dumps({"shots": [ai_shot, rule_shot],
                                                                      "vision_analysis": {"errors": []}}), encoding="utf-8")
        ok, message = batch_worker.validate_visual_match(self.report)
        self.assertTrue(ok, message)
        self.match["segments"][0]["ai_selection"]["observation"] = ""
        (self.report / "shot_match_report.json").write_text(json.dumps(self.match), encoding="utf-8")
        ok, message = batch_worker.validate_visual_match(self.report)
        self.assertFalse(ok, message)


if __name__ == "__main__":
    unittest.main()
