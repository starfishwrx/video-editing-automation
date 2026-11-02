"""Cached paper replay must prefer readable fits and reject uncertain evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import ai_editor_contract
from orchestrator.selection import paper_selection

US = 1_000_000


class PaperSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="paper selection ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "replay"
        self.manifest = {"category": "纸品", "product": "synthetic paper", "fps": 30}
        self.shots = {}
        for name, length in (("preferred", 6), ("exception", 8)):
            source = self.root / (name + ".mp4")
            source.write_bytes(b"Synthetic offline media fixture: " + name.encode())
            self.shots[name] = {"shot_id": name, "video": str(source), "source_start": 0,
                                "source_end": length, "status": "ready_for_matching"}
        self.analysis = {"shots": list(self.shots.values())}
        self.request = {"index": 1, "text": "Synthetic complete action and result",
                        "requirement": "Retain a visible complete action and result",
                        "require_action": True, "segment_us": 3_750_000,
                        "candidate_ids": ["preferred"]}

    def observation(self, shot, confidence=.8):
        start, end = round(shot["source_start"] * US), round(shot["source_end"] * US)
        return {"verdict": "matched", "same_product": True, "supports_requirement": True,
                "confidence": confidence, "observation": "Synthetic visible action and result",
                "reason": "Offline fixture supports the requested action", "time_sensitive": False,
                "window_us": {"start": start, "end": end},
                "evidence_intervals_us": [{"start": start, "end": end}],
                "phases": [
                    {"start": start, "end": start + US, "kind": "key_action", "can_speed": False,
                     "reason": "Essential action remains at original speed"},
                    {"start": start + US, "end": end - 500_000, "kind": "process", "can_speed": True,
                     "reason": "Reviewed nonessential travel can be compressed"},
                    {"start": end - 500_000, "end": end, "kind": "result", "can_speed": False,
                     "reason": "Visible result remains at original speed"}]}

    def replay(self, requests=None, reviewer=None, analysis=None):
        return paper_selection.reselect(
            self.manifest, self.analysis if analysis is None else analysis,
            [self.request] if requests is None else requests, self.directory,
            reviewer=reviewer or (lambda manifest, request, shot, directory: self.observation(shot)))

    def assert_unresolved(self, result, reason="EVIDENCE_NOT_CONFIRMED"):
        self.assertEqual(result["status"], "SELECTION_PARTIAL")
        self.assertEqual(result["overrides"], [])
        self.assertTrue(result["preview_only"])
        self.assertEqual(result["unresolved"], [{"index": 1, "reason": reason}])

    def test_pixel_review_requests_dense_aspect_preserving_media(self):
        from orchestrator.selection import ai_editor
        shot = self.shots["preferred"]
        identity = ai_editor_contract.source_identity(shot["video"])
        sheet = self.root / "portrait sheet.jpg"
        sheet.write_bytes(b"Synthetic prepared pixels")
        media = {"start": 0, "end": 6, "before": identity, "sheet": sheet}
        with mock.patch.object(ai_editor, "_review_media", return_value=media) as prepare, \
                mock.patch.object(ai_editor, "_call", return_value=self.observation(shot)) as call:
            result = paper_selection.review_candidate(self.manifest, self.request, shot, self.directory)
        prepare.assert_called_once_with(shot, self.directory, dense=True, fit=True)
        self.assertEqual(call.call_args.args[2], sheet)
        self.assertEqual(result["source_identity"], identity)
        self.assertEqual(result["reviewed_window_us"], {"start": 0, "end": 6 * US})
        self.assertEqual(result["sheet"], str(sheet))

    def test_preferred_candidate_beats_higher_confidence_three_speed_candidate(self):
        self.request["candidate_ids"] = ["exception", "preferred"]
        def review(manifest, request, shot, directory):
            return self.observation(shot, .99 if shot["shot_id"] == "exception" else .61)
        result = self.replay(reviewer=review)
        self.assertEqual(result["status"], "SELECTION_READY")
        self.assertEqual(result["rows"][0]["selected_shot_id"], "preferred")
        self.assertEqual(result["rows"][0]["tier"], "PREFERRED")
        self.assertEqual(result["rows"][0]["maximum_speed"], 2)
        self.assertEqual(result["rows"][0]["candidates"][0]["plan"]["tier"], "EXCEPTION")
        self.assertTrue(result["preview_only"])
        self.assertEqual(result["preserved"], ["narration", "sentence_duration", "subtitles", "bgm"])

    def test_protected_action_and_result_remain_one_speed_and_sentence_is_exact(self):
        result = self.replay()
        parts = result["overrides"][0]["parts"]
        self.assertEqual([p["speed"] for p in parts], [1, 2, 1])
        self.assertEqual(sum(p["segment_us"] for p in parts), self.request["segment_us"])
        self.assertEqual(parts[0]["source_start_us"], 0)
        self.assertEqual(parts[-1]["source_end_us"], 6 * US)
        self.assertTrue(all(a["source_end_us"] == b["source_start_us"] for a, b in zip(parts, parts[1:])))
        self.assertTrue(all(p["source"] == self.shots["preferred"]["video"] for p in parts))
        persisted = json.loads((self.directory / "selection.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted, result)

    def test_three_speed_is_explicit_exception_without_formal_permission(self):
        self.request["segment_us"] = 3 * US
        result = self.replay()
        self.assertEqual(result["rows"][0]["tier"], "EXCEPTION")
        self.assertEqual(result["rows"][0]["maximum_speed"], 3)
        self.assertEqual(result["overrides"][0]["retiming_plan"]["exception_reason"], "NO_PREFERRED_SPEED_FIT")
        self.assertTrue(result["preview_only"])
        self.assertNotEqual(result["status"], "PASS")

    def test_fully_matching_source_can_still_have_duration_conflict(self):
        self.request["segment_us"] = 2_900_000
        result = self.replay()
        self.assert_unresolved(result, "DURATION_FIT_CONFLICT")
        plan = result["rows"][0]["candidates"][0]["plan"]
        self.assertEqual(plan["reason"], "DURATION_FIT_CONFLICT")
        self.assertEqual(plan["parts"], [])

    def test_time_sensitive_evidence_cannot_be_compressed(self):
        def review(manifest, request, shot, directory):
            return dict(self.observation(shot), time_sensitive=True)
        result = self.replay(reviewer=review)
        self.assert_unresolved(result, "DURATION_FIT_CONFLICT")
        self.assertEqual(result["rows"][0]["candidates"][0]["plan"]["detail"]["protected_us"], 6 * US)

    def test_process_without_explicit_speed_permission_is_also_protected(self):
        def review(manifest, request, shot, directory):
            row = self.observation(shot)
            row["phases"][1]["can_speed"] = False
            return row
        self.assert_unresolved(self.replay(reviewer=review), "DURATION_FIT_CONFLICT")

    def test_acceleration_of_key_action_or_result_is_rejected(self):
        for index in (0, 2):
            def review(manifest, request, shot, directory):
                row = self.observation(shot)
                row["phases"][index]["can_speed"] = True
                return row
            with self.subTest(phase=index):
                result = self.replay(reviewer=review)
                self.assert_unresolved(result)
                self.assertEqual(result["rows"][0]["candidates"][0]["error"],
                                 "PAPER_REVIEW_PROTECTED_PHASE_ACCELERATED")

    def test_source_stat_change_during_review_never_creates_override(self):
        def review(manifest, request, shot, directory):
            row = self.observation(shot)
            path = Path(shot["video"])
            path.write_bytes(path.read_bytes() + b"changed")
            return row
        result = self.replay(reviewer=review)
        self.assert_unresolved(result)
        self.assertEqual(result["rows"][0]["candidates"][0]["error"], "PAPER_SOURCE_CHANGED_DURING_REVIEW")

    def test_old_source_identity_is_not_relabelled_as_fresh(self):
        old = ai_editor_contract.source_identity(self.shots["preferred"]["video"])
        Path(self.shots["preferred"]["video"]).write_bytes(b"A changed media fixture with another size")
        def review(manifest, request, shot, directory):
            return dict(self.observation(shot), source_identity=old)
        result = self.replay(reviewer=review)
        self.assert_unresolved(result)
        self.assertEqual(result["rows"][0]["candidates"][0]["error"], "PAPER_REVIEW_STALE_SOURCE")

    def test_unavailable_source_is_uncertified_not_a_successful_cached_match(self):
        Path(self.shots["preferred"]["video"]).unlink()
        result = self.replay()
        self.assert_unresolved(result)
        self.assertEqual(result["rows"][0]["candidates"][0]["plan"]["reason"], "REVIEW_UNCERTIFIED")

    def test_unknown_not_ready_and_duplicate_requested_candidates_are_rejected_before_review(self):
        called = []
        def review(*args):
            called.append(True)
            self.fail("Invalid candidate reached pixel review")
        for ids, reason in ((["unknown"], "PAPER_CANDIDATE_NOT_READY"),
                            (["preferred", "preferred"], "PAPER_REPLAY_REQUEST_INVALID"),
                            ([], "PAPER_REPLAY_REQUEST_INVALID"),
                            (["a", "b", "c", "d"], "PAPER_REPLAY_REQUEST_INVALID")):
            with self.subTest(ids=ids):
                self.request["candidate_ids"] = ids
                with self.assertRaisesRegex(ValueError, reason):
                    self.replay(reviewer=review)
        self.request["candidate_ids"] = ["preferred"]
        not_ready = copy.deepcopy(self.analysis)
        not_ready["shots"][0]["status"] = "pending_analysis"
        with self.assertRaisesRegex(ValueError, "PAPER_CANDIDATE_NOT_READY"):
            self.replay(reviewer=review, analysis=not_ready)
        self.assertEqual(called, [])

    def test_duplicate_sentence_identity_is_rejected_before_any_review(self):
        with self.assertRaisesRegex(ValueError, "PAPER_REPLAY_REQUEST_INVALID"):
            self.replay(requests=[self.request, dict(self.request)],
                        reviewer=lambda *args: self.fail("Duplicate sentence was reviewed"))

    def test_duplicate_cached_shot_identity_cannot_silently_select_a_different_source(self):
        duplicate = dict(self.shots["exception"], shot_id="preferred")
        with self.assertRaisesRegex(ValueError, "PAPER_.*(?:IDENTITY|DUPLICATE|SHOT)"):
            self.replay(analysis={"shots": [self.shots["preferred"], duplicate]},
                        reviewer=lambda *args: self.fail("Ambiguous shot identity reached review"))

    def test_unhashable_candidate_identifier_is_a_request_error(self):
        self.request["candidate_ids"] = [["preferred"]]
        with self.assertRaisesRegex(ValueError, "PAPER_REPLAY_REQUEST_INVALID"):
            self.replay(reviewer=lambda *args: self.fail("Malformed ID reached review"))

    def test_status_only_or_incomplete_responses_cannot_create_selection(self):
        values = [{"status": "PASS"}, {"verdict": "matched"},
                  {"verdict": "uncertain"}, {"verdict": "mismatch"}]
        for value in values:
            with self.subTest(value=value):
                self.assert_unresolved(self.replay(reviewer=lambda *args: copy.deepcopy(value)))

    def test_nondictionary_review_response_is_isolated_as_uncertified(self):
        for value in (None, [], "PASS", True):
            with self.subTest(value=value):
                result = self.replay(reviewer=lambda *args: value)
                self.assert_unresolved(result)
                self.assertEqual(result["rows"][0]["candidates"][0]["plan"]["reason"], "REVIEW_UNCERTIFIED")

    def test_bad_candidate_response_does_not_discard_a_valid_alternative(self):
        self.request["candidate_ids"] = ["exception", "preferred"]
        def review(manifest, request, shot, directory):
            return None if shot["shot_id"] == "exception" else self.observation(shot)
        result = self.replay(reviewer=review)
        self.assertEqual(result["rows"][0]["selected_shot_id"], "preferred")
        self.assertEqual(result["status"], "SELECTION_READY")
        self.assertEqual(result["rows"][0]["candidates"][0]["plan"]["reason"], "REVIEW_UNCERTIFIED")

    def test_noncontinuous_phase_plan_and_outside_evidence_are_not_accepted(self):
        for alteration in ("gap", "overlap", "unfinished", "evidence", "window", "permission"):
            def review(manifest, request, shot, directory):
                row = self.observation(shot)
                if alteration == "gap": row["phases"][1]["start"] += 1
                elif alteration == "overlap": row["phases"][1]["start"] -= 1
                elif alteration == "unfinished": row["phases"][-1]["end"] -= 1
                elif alteration == "evidence": row["evidence_intervals_us"][0]["end"] += 1
                elif alteration == "window": row["window_us"]["start"] = True
                elif alteration == "permission": row["phases"][1]["can_speed"] = 1
                return row
            with self.subTest(alteration=alteration):
                self.assert_unresolved(self.replay(reviewer=review))

    def test_require_action_prevents_passing_a_display_only_observation(self):
        def review(manifest, request, shot, directory):
            row = self.observation(shot)
            row["phases"][0]["kind"] = "display"
            return row
        result = self.replay(reviewer=review)
        self.assert_unresolved(result)
        self.assertEqual(result["rows"][0]["candidates"][0]["error"], "PAPER_REVIEW_KEY_ACTION_MISSING")

    def test_require_result_rejects_action_without_a_visible_result_phase(self):
        self.request["require_result"] = True
        def review(manifest, request, shot, directory):
            row = self.observation(shot)
            row["phases"][-1]["kind"] = "display"
            return row
        result = self.replay(reviewer=review)
        self.assert_unresolved(result)
        self.assertEqual(result["rows"][0]["candidates"][0]["error"], "PAPER_REVIEW_RESULT_MISSING")
        self.assertEqual(self.replay()["status"], "SELECTION_READY")

    def test_review_region_hint_is_reobserved_and_cannot_escape_cached_scene(self):
        self.request["candidate_windows"] = {"preferred": {"start": 500_000, "end": 5_500_000}}
        observed = []
        def review(manifest, request, shot, directory):
            observed.append((shot["source_start"], shot["source_end"]))
            return self.observation(shot)
        result = self.replay(reviewer=review)
        self.assertEqual(observed, [(.5, 5.5)])
        self.assertEqual(result["overrides"][0]["parts"][0]["source_start_us"], 500_000)
        self.request["candidate_windows"]["preferred"]["end"] = 6_000_001
        with self.assertRaisesRegex(ValueError, "PAPER_REVIEW_HINT_INVALID"):
            self.replay(reviewer=lambda *args: self.fail("Out-of-scene hint reached review"))

    def test_replay_does_not_mutate_cached_observations_requests_or_manifest(self):
        before = copy.deepcopy((self.manifest, self.analysis, self.request))
        self.replay()
        self.assertEqual((self.manifest, self.analysis, self.request), before)

    def test_bad_request_container_or_scalar_fields_cannot_reach_reviewer(self):
        for requests in ([], "one", [None], [True], [{**self.request, "index": True}],
                         [{**self.request, "segment_us": True}], [{**self.request, "text": " "}]):
            with self.subTest(requests=requests):
                with self.assertRaisesRegex(ValueError, "PAPER_REPLAY_REQUEST_INVALID"):
                    self.replay(requests=requests, reviewer=lambda *args: self.fail("Bad request was reviewed"))

    def test_wrong_candidate_window_container_is_rejected_before_review(self):
        for windows in ([{"start": 0, "end": 6 * US}], "unknown"):
            self.request["candidate_windows"] = windows
            with self.subTest(windows=windows):
                with self.assertRaisesRegex(ValueError, "PAPER_.*(?:HINT|REQUEST)_INVALID"):
                    self.replay(reviewer=lambda *args: self.fail("Invalid hint container was reviewed"))

    def test_nonfinite_or_unbounded_confidence_is_uncertified_and_isolated(self):
        for confidence in (True, float("nan"), float("inf"), 10**400, -.1, 1.1):
            def review(manifest, request, shot, directory):
                return self.observation(shot, confidence)
            with self.subTest(confidence=confidence):
                self.assert_unresolved(self.replay(reviewer=review))

    def test_other_category_cannot_enter_paper_replay(self):
        self.manifest["category"] = "卫品"
        with self.assertRaisesRegex(ValueError, "PAPER_SELECTION_CATEGORY_REQUIRED"):
            self.replay()


if __name__ == "__main__":
    unittest.main()
