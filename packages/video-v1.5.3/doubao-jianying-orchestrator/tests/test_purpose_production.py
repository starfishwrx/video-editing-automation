from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.vision import purpose_review as vision
from orchestrator.selection import purpose_selection as selection
from orchestrator.application import purpose_production as app
from orchestrator.media.frame_boundaries import display_start
from orchestrator.domain.timeline import Asset,Canvas,EditTimeline,VideoEvent,TimelineError
from orchestrator.media.timeline_render import _frame_plan


def observed():
    return {"verdict":"matched","same_product":True,"supports_requirement":True,"confidence":.8,
        "observation":"visible contact and result","reason":"observed","time_sensitive":False,
        "window_us":{"start":0,"end":4_000_000},"evidence_intervals_us":[{"start":0,"end":4_000_000}],
        "phases":[{"start":0,"end":1_000_000,"kind":"key_action","can_speed":False,"reason":"contact"},
            {"start":1_000_000,"end":3_000_000,"kind":"process","can_speed":True,"reason":"repetitive process"},
            {"start":3_000_000,"end":4_000_000,"kind":"result","can_speed":False,"reason":"result"}],
        "reviewed_window_us":{"start":0,"end":5_000_000},"supported_ids":["c1"]}


class PurposeEvidenceTests(unittest.TestCase):
    def test_output_review_uses_assigned_picture_frames_before_neighboring_cut(self):
        from test_timeline_export import example
        timeline=example()
        first,last=app.picture_window(timeline,timeline.sentences[0])
        self.assertEqual((first,last),(0,333333))
        self.assertLess(last,timeline.sentences[0].duration_us)
        self.assertEqual(app.picture_window(timeline,timeline.sentences[-1])[1],733333)
    def test_black_placeholder_cannot_pass_from_neighboring_cut_sample(self):
        from test_timeline_export import example
        from dataclasses import replace
        timeline=replace(example(),unresolved=({"sentence_id":"s1","reason":"black is diagnostic only"},))
        raw={"index":1,"status":"SAMPLED_SUPPORT","reason":"neighboring frame looks relevant"}
        guarded=app.guard_placeholder(timeline,raw)
        self.assertEqual(guarded["status"],"EVIDENCE_PENDING")
        self.assertEqual(guarded["raw_model_status"],"SAMPLED_SUPPORT")
    def tail_fixture(self, *, mode="preview", count=60):
        event=VideoEvent("last","v",(),360000,115000,0,115000)
        timeline=EditTimeline("fixture",1,Canvas(),475000,(Asset("v","source", "video",1000000,108,192,60),),
            (),(),delivery_mode=mode)
        metadata={"stream":{"avg_frame_rate":"60/1","time_base":"1/60000"},
            "source_end_us":round(count*1000000/60),"frames":[{"pts_us":round(i*1000000/60)}for i in range(count)]}
        return timeline,event,metadata

    def test_real_tail_is_explicit_preview_only_and_records_actual_extra_frame(self):
        timeline,event,metadata=self.tail_fixture()
        with self.assertRaisesRegex(TimelineError,"OUTSIDE"):
            _frame_plan(timeline,event,metadata)
        frames=_frame_plan(timeline,event,metadata,allow_real_tail=True)
        self.assertEqual(frames[-1]["source_pts_us"],133333)
        self.assertEqual(frames[-1]["preview_real_tail_us"],18333)
        self.assertEqual(len({f["source_frame"]for f in frames}),len(frames))

    def test_real_tail_never_fakes_missing_source_or_relaxes_formal(self):
        timeline,event,metadata=self.tail_fixture(mode="formal")
        with self.assertRaisesRegex(TimelineError,"OUTSIDE"):
            _frame_plan(timeline,event,metadata,allow_real_tail=True)

    def test_preview_cannot_borrow_two_tail_frames_from_source(self):
        timeline,event,metadata=self.tail_fixture(count=180)
        from dataclasses import replace
        timeline=replace(timeline,duration_us=2033667)
        event=replace(event,target_start_us=1033000,duration_us=1000667,source_start_us=8333,source_duration_us=1000667)
        with self.assertRaisesRegex(TimelineError,"OUTSIDE"):
            _frame_plan(timeline,event,metadata,allow_real_tail=True)
        timeline,event,metadata=self.tail_fixture(count=7)
        with self.assertRaisesRegex(TimelineError,"REPEATED"):
            _frame_plan(timeline,event,metadata,allow_real_tail=True)
    def test_real_time_display_uses_actual_frame_without_changing_duration(self):
        self.assertEqual(display_start(17001,850952,[0,16667,33333,50000],1000000),16667)
        self.assertEqual(display_start(10001,850952,[0,16667,33333,50000],1000000),16667)

    def test_display_alignment_cannot_jump_or_run_past_source_end(self):
        with self.assertRaisesRegex(ValueError,"INFEASIBLE"):
            display_start(50000,1000000,[0,16667,33333,50000],1000000)
    def binding_fixture(self, raw):
        voice=Path(raw)/"voice.wav";voice.write_bytes(b"original audio")
        manifest={"task_id":"fixture","product":"fixture","category":"fixture","full_script":"原文",
            "segments":[{"text":"原文"}],"voice":"selected voice","material_sources":["current.mov"]}
        narration={"voice":"selected voice","rows":[{"index":1,"text":"原文","tts_path":str(voice),"audio_duration_us":1000000}]}
        artifact={"task_id":"fixture","sources":[str(Path("current.mov").resolve())],
            "context":app._context(manifest),"narration":app._audio_binding(narration),
            "requests":[{"index":1,"text":"原文"}]}
        return manifest,narration,artifact,voice

    def test_same_length_audio_replacement_invalidates_prior_selection(self):
        with tempfile.TemporaryDirectory() as raw:
            manifest,narration,artifact,voice=self.binding_fixture(raw)
            before=voice.stat();voice.write_bytes(b"replaced audio")
            os.utime(voice,ns=(before.st_atime_ns,before.st_mtime_ns+10000000))
            with self.assertRaisesRegex(ValueError,"INPUT_CHANGED"):
                app._binding(manifest,narration,artifact)

    def test_changed_product_or_voice_cannot_reuse_checklist(self):
        with tempfile.TemporaryDirectory() as raw:
            manifest,narration,artifact,_=self.binding_fixture(raw)
            for key in ("product","voice","full_script","category"):
                with self.subTest(key=key),self.assertRaisesRegex(ValueError,"INPUT_CHANGED"):
                    app._binding({**manifest,key:"changed"},narration,artifact)

    def test_missing_output_sentence_stops_before_media_or_api(self):
        from test_timeline_export import example
        with patch.object(app,"check_production") as check:
            with self.assertRaisesRegex(ValueError,"REQUEST_COVERAGE"):
                app.review_render({"task_id":"synthetic-export"},example(),[],Path("missing"),Path("unused"))
            check.assert_not_called()

    def test_changed_output_receipt_stops_before_observation(self):
        from test_timeline_export import example
        timeline=example()
        requests=[{"index":i,"text":s.text} for i,s in enumerate(timeline.sentences,1)]
        with patch.object(app,"check_production",return_value={"structure_media":{"status":"FAIL"}}):
            with self.assertRaisesRegex(ValueError,"RENDER_IDENTITY"):
                app.review_render({"task_id":"synthetic-export"},timeline,requests,Path("missing"),Path("unused"))

    def test_check_coverage_cannot_drop_compound_claim(self):
        expected=[{"id":"c1","type":"display"},{"id":"c2","type":"label"}]
        with self.assertRaisesRegex(ValueError,"COVERAGE"):
            vision.check_results([{"id":"c1","status":"supported","observed":"seen"}],expected)

    def test_repair_retains_occupied_source_of_unrepairable_row(self):
        from test_timeline_export import example
        def candidate(sid,source):
            return {"shot_id":sid,"source":source,"observation":observed(),"plan":{
                "ok":True,"source_start_us":0,"source_end_us":2000000,"tier":"ORIGINAL","maximum_speed":1}}
        a,b=candidate("a","a.mov"),candidate("b","b.mov")
        previous={"rows":[{"index":1,"selected":a,"candidates":[a]},
            {"index":2,"selected":b,"candidates":[a,b]}]}
        review={"protocol":vision.OUTPUT_PROTOCOL,"rows":[{"index":i,"text":"重复字幕","status":"EVIDENCE_PENDING",
            "uncovered_claims":["missing duration claim"] if i == 1 else []}for i in (1,2)],
            "timeline_identity":app.timeline_identity(example()),"output_identity":{"size":1}}
        with tempfile.TemporaryDirectory() as raw,patch.object(app,"_binding"):
            result=app.repair_selection({}, {},previous,review,example(),Path(raw)/"repair2")
        self.assertEqual(result["changed_indices"],[])
        self.assertEqual(result["rows"][1]["selected"]["source"],"b.mov")

    def test_repair_never_overwrites_prior_round(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/"selection.json").write_text("original",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"OUTPUT_EXISTS"):
                app.repair_selection({}, {},{}, {},None,root)
            self.assertEqual((root/"selection.json").read_text(encoding="utf-8"),"original")

    def test_unverifiable_claim_never_becomes_supported(self):
        with self.assertRaisesRegex(ValueError,"UNVERIFIABLE"):
            vision.check_results([{"id":"c1","status":"supported","observed":"all day"}],
                [{"id":"c1","type":"unverifiable"}])

    def test_unknown_check_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"IDENTITY"):
            vision.check_results([{"id":"another","status":"uncertain","observed":"unknown"}],
                [{"id":"c1","type":"display"}])

    def test_partial_support_is_not_complete_coverage(self):
        self.assertEqual(vision.check_results([
            {"id":"c1","status":"supported","observed":"thin edge"},
            {"id":"c2","status":"uncertain","observed":"no breathing test"}],
            [{"id":"c1","type":"display"},{"id":"c2","type":"action_result"}]),["c1"])

    def test_phase_fit_keeps_action_and_result_original_speed(self):
        request={"segment_us":3_000_000,"require_action":True,"require_result":True}
        result=selection.fit(observed(),{"source_start":0,"source_end":5},request)
        self.assertTrue(result["ok"])
        self.assertEqual([p["speed"] for p in result["parts"]],[1,2,1])

    def test_speed_ceiling_cannot_hide_conflict(self):
        result=selection.fit(observed(),{"source_start":0,"source_end":5},
            {"segment_us":2_100_000,"require_action":True,"require_result":True})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"],"DURATION_FIT_CONFLICT")

    def test_time_sensitive_process_is_not_accelerated(self):
        row=observed();row["time_sensitive"]=True
        result=selection.fit(row,{"source_start":0,"source_end":5},
            {"segment_us":3_000_000,"require_action":True,"require_result":True})
        self.assertFalse(result["ok"])

    def test_accelerated_key_action_is_rejected(self):
        row=observed();row["phases"][0]["can_speed"]=True
        with self.assertRaisesRegex(ValueError,"PROTECTED"):
            selection.fit(row,{"source_start":0,"source_end":5},
                {"segment_us":3_000_000,"require_action":True,"require_result":True})

    def test_allocator_does_not_replay_same_source_interval(self):
        candidate={"source":"source.mov","observation":observed(),"plan":{
            "ok":True,"source_start_us":0,"source_end_us":4_000_000,"tier":"ORIGINAL","maximum_speed":1}}
        self.assertIsNone(selection.choose([candidate],[{"source":"source.mov","start":3_000_000,"end":5_000_000}]))

    def test_wrong_source_scope_stops_before_api(self):
        with tempfile.TemporaryDirectory() as raw,patch.object(vision,"requirements") as call:
            with self.assertRaisesRegex(ValueError,"SOURCE_SCOPE"):
                app.inspect({"material_sources":["current.mov"]},{"shots":[{"shot_id":"v","video":"old.mov"}]},
                    {"rows":[]},Path(raw)/"inspect")
            call.assert_not_called()

    def test_preparation_values_reach_consumer_and_errors_remain_unknown(self):
        jobs=[1,2,3]
        result=list(app._parallel(jobs,lambda j:j*10,lambda prepared:{"value":prepared+1},workers=2))
        self.assertEqual(dict((j,r["value"]) for j,r in result),{1:11,2:21,3:31})

    def test_output_reviewer_detects_compiler_omission(self):
        request={"index":1,"text":"thin and stays dry all day","checks":[{"id":"c1","type":"display"}],
            "require_action":False,"require_result":False}
        data={"checks":[{"id":"c1","status":"supported","observed":"thin"}],"action_complete":True,
            "result_visible":True,"rhythm_ok":True,"all_script_claims_checked":True,
            "uncovered_claims":["all-day dryness not shown"],"reason":"thin only"}
        media={"start":0,"end":1,"before":{"size":1},"sheet":"sheet.jpg"}
        with patch.object(vision.ai_editor,"_call",return_value=data),patch.object(vision.contract,"source_identity",return_value=media["before"]):
            result=vision.review_output({"product":"fixture"},request,{"video":"preview.mp4"},media,Path("unused"))
        self.assertEqual(result["status"],"EVIDENCE_PENDING")


if __name__ == "__main__":
    unittest.main()
