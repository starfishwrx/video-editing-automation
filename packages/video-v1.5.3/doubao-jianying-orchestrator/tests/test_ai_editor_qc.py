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
        self.match["manifest"]["delivery_mode"] = "formal"
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

    def test_current_saved_draft_qc_and_standalone_consumer_both_pass(self):
        code, result = self.run_qc()
        self.assertEqual((code, result["status"]), (0, "PASS"), result)
        self.assertEqual(result["delivery_mode"], "formal")
        self.assertEqual(result["release_eligibility"], "SHIP_ALLOWED")
        self.assertEqual(visual_qc_contract.report_status(result), "PASS")
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from tools import production_eval
        self.assertTrue(production_eval.visual_qc_contract.report_matches_draft(result, self.draft))
        self.fixture.video.write_bytes(b"source changed after QC")
        self.assertFalse(production_eval.visual_qc_contract.report_matches_draft(result, self.draft))

    def test_upright_working_copy_keeps_original_binding_and_invalidates_on_mapping_change(self):
        from orchestrator.media import source_normalization as prepared
        source = self.fixture.video
        actual = self.root / "upright.mp4"
        actual.write_bytes(b"synthetic transformed pixels with different bytes")
        receipt = {"schema": prepared.VERSION, "source": contract.source_identity(source),
            "prepared": prepared.identity(actual), "prepared_sha256": prepared.file_digest(actual),
            "lossless": False, "quality": {"mode": prepared.QUALITY_MODE}, "rotation_degrees": 90, "frame_clock": {"count": 60},
            "time_mapping": {"offset_us": 0, "rate_num": 1, "rate_den": 1}}
        path = self.draft / prepared.RECEIPT_FILE
        path.write_text(json.dumps({"schema": prepared.VERSION, "sources": [receipt]}), encoding="utf-8")
        info = json.loads((self.draft / "draft_info.json").read_text(encoding="utf-8"))
        info["materials"]["videos"][0]["path"] = str(actual)
        (self.draft / "draft_info.json").write_text(json.dumps(info), encoding="utf-8")
        code, result = self.run_qc()
        self.assertEqual((code, result["status"]), (0, "PASS"), result)
        self.assertTrue(visual_qc_contract.report_matches_draft(result, self.draft))
        self.assertEqual({str(Path(r["path"]).resolve()) for r in result["source_bindings"]},
                         {str(source.resolve()), str(actual.resolve())})
        receipt["time_mapping"]["offset_us"] = 1
        path.write_text(json.dumps({"schema": prepared.VERSION, "sources": [receipt]}), encoding="utf-8")
        self.assertFalse(visual_qc_contract.report_matches_draft(result, self.draft))
        code, rejected = self.run_qc()
        self.assertNotEqual(code, 0)
        self.assertIn("AI_QC_SOURCE_CONTENT_CHANGED", rejected["issues"])

    def test_preview_with_all_pixels_matching_never_becomes_formal_pass(self):
        self.match["manifest"]["delivery_mode"] = "preview"
        self.write_reports()
        code, result = self.run_qc()
        self.assertEqual((code, result["status"]), (2, "UNCERTIFIED"))
        self.assertEqual(result["delivery_mode"], "preview")
        self.assertEqual(result["visual_observation_status"], "PASS")
        self.assertEqual(result["release_eligibility"], "PREVIEW_ONLY")
        self.assertIn("UPSTREAM_DELIVERY_MODE_PREVIEW", result["preview_only_reasons"])
        self.assertEqual(visual_qc_contract.report_status(result), "UNCERTIFIED")
        result["status"] = "PASS"  # persisted status cannot erase preview provenance
        self.assertEqual(visual_qc_contract.report_status(result), "UNCERTIFIED")

    def test_v4_requires_explicit_build_mode_in_match_and_persisted_report(self):
        for kind in ("missing", "invalid", "conflict"):
            with self.subTest(kind=kind):
                self.match.pop("delivery_mode", None)
                self.match["manifest"].pop("delivery_mode", None)
                if kind == "invalid":
                    self.match["manifest"]["delivery_mode"] = "junk"
                elif kind == "conflict":
                    self.match["delivery_mode"] = "formal"
                    self.match["manifest"]["delivery_mode"] = "preview"
                self.write_reports()
                code, result = self.run_qc()
                self.assertEqual((code, result["status"]), (2, "UNCERTIFIED"))
                self.assertEqual(result["delivery_mode"], "unknown")
                self.assertEqual(result["visual_observation_status"], "PASS")
                self.assertEqual(result["release_eligibility"], "BLOCKED")
                self.assertEqual(visual_qc_contract.report_status(result), "UNCERTIFIED")
        self.match.pop("delivery_mode", None)
        self.match["manifest"]["delivery_mode"] = "formal"
        self.write_reports()
        _, result = self.run_qc()
        result.pop("delivery_mode")
        self.assertEqual(visual_qc_contract.report_status(result), "UNCERTIFIED")

    def test_legacy_response_cannot_certify_ai_draft(self):
        for version in (visual_qc_contract.RESPONSE_VERSION, "chatcut-visual-qc-response-v4"):
            with self.subTest(version=version):
                code, result = self.run_qc(version)
                self.assertEqual((code, result["status"]), (2, "UNCERTIFIED"), result)

    def test_recovery_audio_binding_and_native_selection_identity_reach_qc_consumer(self):
        from orchestrator.application import recovery_audio
        from orchestrator.selection import timing_contract
        audio = self.root / "retained.wav"
        audio.write_bytes(b"retained audio fixture")
        timing = self.root / "timing.json"
        timing.write_text(json.dumps({"rows": [{"key": timing_contract.sentence_key(self.segment, 1),
            "text": self.segment["text"], "tts_path": str(audio), "audio_duration_us": 2_000_000,
            "timing_certainty": "audio"}]}), encoding="utf-8")
        manifest = {**self.match["manifest"], "narration_timing_file": str(timing)}
        bound = recovery_audio.bind_and_record(manifest, self.match, self.report)
        identity_path = self.draft / "ai_selection_identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        # This is the identity written by the native production backend.
        identity["selection_sha256"] = contract.digest(bound["segments"])
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        code, result = self.run_qc()
        self.assertEqual((code, result["status"]), (0, "PASS"), result)
        self.assertEqual(visual_qc_contract.report_status(result), "PASS")
        self.assertTrue(visual_qc_contract.report_matches_draft(result, self.draft))

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

    def test_blind_labels_do_not_turn_key_frames_into_fallback_samples(self):
        from orchestrator.quality import draft_visual_qc as final_qc
        from unittest import mock
        for suffix in ('p1_phase0_reveal','p1_phase0_readable','p1_ev0_result'):
            item={'suffix':suffix,'timestamp':1.5}
            self.assertEqual(ai_editor_qc.blind_frame_label(1,2,item),'S01 P1 F02 @1.500s')
            with mock.patch.object(final_qc,'_resume_frame',return_value=(False,'missing')) as extract:
                ok,_error,timestamp=final_qc._resume_sample_frame('ffmpeg',Path('source.mp4'),1.5,
                    Path('frame.jpg'),source_start=1.,suffix=suffix)
            self.assertFalse(ok)
            self.assertEqual(timestamp,1.5)
            extract.assert_called_once()
        with mock.patch.object(final_qc,'_resume_frame',side_effect=[(False,'missing'),(True,'')]) as extract:
            ok,_error,timestamp=final_qc._resume_sample_frame('ffmpeg',Path('source.mp4'),1.5,
                Path('frame.jpg'),source_start=1.,suffix='p1_t3')
        self.assertTrue(ok)
        self.assertEqual(timestamp,1.46)
        self.assertEqual(extract.call_count,2)

    def test_key_frame_wins_a_coincident_ordinary_sample_and_cannot_fall_back(self):
        from orchestrator.quality import draft_visual_qc as final_qc
        from unittest import mock
        row={'source_start':0.,'source_duration':1.}
        claim={'ai_segment':{'ai_selection':{'evidence_intervals_us':[{'start':140_000,'end':500_000}]}}}
        frames=ai_editor_qc.frame_plan(claim,[row])
        matching=[(suffix,fraction) for _,suffix,fraction in frames if abs(fraction-.14)<1e-9]
        self.assertEqual(len(matching),1)
        suffix,fraction=matching[0]
        with mock.patch.object(final_qc,'_resume_frame',return_value=(False,'missing')) as extract:
            ok,_error,timestamp=final_qc._resume_sample_frame('ffmpeg',Path('source.mp4'),fraction,
                Path('frame.jpg'),source_start=0.,suffix=suffix)
        self.assertFalse(ok)
        self.assertEqual(timestamp,.14)
        extract.assert_called_once()

    def test_static_match_cannot_waive_a_required_presentation_action(self):
        from test_presentation_windows import phase
        claim={'ai_segment':{'ai_selection':{'presentation_observation':phase()}}}
        row={'index':1,'match_level':'DIRECT','visual_ok':True,'reason':'普通促销陈列足够','observed':'实际商品画面','confidence':.9}
        good={'actor':'product','change_visible':True,'result_visibility':'clear','observation':'实际看见商品转正及正面到位'}
        for check in (None,{**good,'actor':'camera'},{**good,'actor':'hand_only'},
                      {**good,'change_visible':False},{**good,'result_visibility':'partial'},
                      {**good,'actor':'occluder'},{**good,'observation':''}):
            gated=ai_editor_qc.apply_presentation_gate({**row,'action_check':check},claim)
            self.assertEqual(gated['match_level'],'INSUFFICIENT_EVIDENCE')
            self.assertFalse(gated['visual_ok'])
        self.assertEqual(ai_editor_qc.apply_presentation_gate({**row,'action_check':good},claim)['match_level'],'DIRECT')
        self.assertEqual(ai_editor_qc.apply_presentation_gate(row,{'ai_segment':{}}),row)
        accepted=ai_editor_qc.apply_presentation_gate({**row,'action_check':good},claim)
        self.assertEqual(visual_qc_contract.row_errors(accepted),[])
        accepted.pop('action_check')
        self.assertIn('PRESENTATION_ACTION_UNVERIFIED',visual_qc_contract.row_errors(accepted))

    def test_persisted_action_gate_uses_bound_selection_not_deletable_markers(self):
        import copy
        from test_presentation_windows import phase
        _, result = self.run_qc()
        result['selection_segments'][0]['ai_selection']['presentation_observation'] = phase()
        result['selection_sha256'] = contract.digest(result['selection_segments'])
        row=result['model_review'][0]
        row.update(presentation_check_required=True,presentation_expected_actors=['product'],
            action_check=dict(actor='product',change_visible=True,result_visibility='clear',observation='商品转正到位'))
        self.assertEqual(visual_qc_contract.report_status(result),'PASS')
        # Self-consistency cannot authenticate a changed selection to the build.
        self.assertFalse(visual_qc_contract.report_matches_draft(result,self.draft))
        for key,value in [('presentation_check_required',None),('presentation_check_required',False),
                          ('presentation_expected_actors',[]),('action_check',None)]:
            edited=copy.deepcopy(result)
            if value is None:
                edited['model_review'][0].pop(key)
            else:
                edited['model_review'][0][key]=value
            self.assertEqual(visual_qc_contract.report_status(edited),'UNCERTIFIED')
        edited=copy.deepcopy(result)
        edited['selection_segments'][0]['ai_selection'].pop('presentation_observation')
        self.assertEqual(visual_qc_contract.report_status(edited),'UNCERTIFIED')
        edited=copy.deepcopy(result)
        edited.pop('selection_segments')
        self.assertFalse(visual_qc_contract.report_matches_draft(edited,self.draft))

    def test_continuous_group_cannot_disagree_about_shared_action(self):
        from test_presentation_windows import phase
        group={'input':{'presentation_observations':[phase()]}}
        segments=[dict(selection_mode='ai_editor',continuity_group=group) for _ in range(2)]
        rows=[dict(index=i,presentation_check_required=True,presentation_expected_actors=['product'],
            action_check=dict(actor=actor,change_visible=True,result_visibility='clear',observation='商品展示'))
            for i,actor in [(1,'product'),(2,'product_and_occluder')]]
        errors=visual_qc_contract.presentation_selection_errors(segments,rows)
        self.assertEqual(set(errors),{1,2})
        self.assertTrue(all('PRESENTATION_GROUP_CHECK_INCONSISTENT' in codes for codes in errors.values()))

    def test_reveal_requires_the_observed_subject_not_a_kind_based_assumption(self):
        from test_presentation_windows import phase
        for actor in ('product','occluder'):
            observation=phase(change=dict(kind='reveal',actor=actor,before='商品被遮住',after='商品完整露出',product_state_changed=True))
            claim={'ai_segment':{'ai_selection':{'presentation_observation':observation}}}
            row=dict(index=1,match_level='DIRECT',visual_ok=True,reason='露出到位',
                action_check=dict(actor=actor,change_visible=True,result_visibility='clear',observation='相对位置变化，商品完整露出'))
            checked=ai_editor_qc.apply_presentation_gate(row,claim)
            self.assertEqual(checked['match_level'],'DIRECT')
            self.assertEqual(checked['presentation_expected_actors'],[actor])

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
