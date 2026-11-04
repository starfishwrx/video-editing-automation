"""Persisted QC must pass the response contract at every automatic consumer."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "doubao-jianying-orchestrator"))
import batch_worker as worker
from tools import production_eval as pe


def source_bindings_for(draft):
    media = Path(draft) / "media" / "qc-source.mp4"
    media.parent.mkdir(exist_ok=True)
    if not media.exists():
        media.write_bytes(b"synthetic current visual source")
    info = media.stat()
    return [{"path": str(media), "identity": {"path": str(media.resolve()),
             "size": info.st_size, "mtime_ns": info.st_mtime_ns}}]


def qc_report(status="PASS", draft=None):
    degraded = status == "PASS_WITH_DEGRADED"
    result = {"version": pe.visual_qc_contract.REPORT_VERSION, "status": status, "delivery_mode": "formal",
            "issues": [], "response_contract": {"version": pe.visual_qc_contract.RESPONSE_VERSION,
                "status": "VALIDATED", "validated_batches": [1], "errors": []},
            "segment_map": [{"claim_index": 1, "draft_segments": [1]}],
            "batch_results": [{"batch_no": 1, "claim_indices": [1], "status": "PARSED",
                               "contract_status": "VALIDATED"}],
            "model_review": [{"index": 1, "match_level": "ACCEPTABLE_DEGRADED" if degraded else "DIRECT",
                "visual_ok": True, "confidence": 0.8, "observed": "合成的测试观察",
                "reason": "合成观察对应测试要求"}]}
    if draft is not None:
        result["draft"] = str(draft)
        result["source_bindings"] = source_bindings_for(draft)
        fingerprint = pe.visual_qc_contract.draft_metadata_fingerprint(draft)
        result["draft_metadata_sha256"] = fingerprint
        result["draft_metadata_sha256_final"] = fingerprint
    return result


class VisualQcDeliveryContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qc-delivery-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.drafts = self.root / "drafts"
        self.draft = self.drafts / "draft-a"
        self.draft.mkdir(parents=True)
        (self.draft / "draft_info.json").write_text(json.dumps({"id": "draft-id-a", "tracks": []}))
        self.report_dir = self.root / "report"
        self.report_dir.mkdir()
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"synthetic media identity")
        self.task = {"task_id": "qc-task", "record_id": "qc-record", "category": "paper", "sku": "SKU-QC",
                     "script": "合成测试脚本", "delivery_mode": "formal"}
        self.runtime = {"tool_version": "1.5.6", "visual_policy_version": "test",
                        "package_sha256": "a" * 64, "visual_policy_sha256": "b" * 64}
        self.contract = pe.freeze(self.task, "qc-test-run", [self.source], self.runtime)

    def write_report(self, payload):
        target = self.report_dir / "draft_visual_qc.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def run_worker(self, payload, *, returncode=0):
        payload = dict(payload)
        payload.setdefault("draft", str(self.draft))
        payload.setdefault("source_bindings", source_bindings_for(Path(payload["draft"])))
        fingerprint = pe.visual_qc_contract.draft_metadata_fingerprint(Path(payload["draft"]))
        payload.setdefault("draft_metadata_sha256", fingerprint)
        payload.setdefault("draft_metadata_sha256_final", payload["draft_metadata_sha256"])
        self.write_report(payload)
        script = self.root / "qc.py"
        script.touch()
        with mock.patch.object(worker, "POST_DRAFT_QC", script), \
                mock.patch.object(worker, "SKILL", self.root), \
                mock.patch.object(worker.subprocess, "run", return_value=SimpleNamespace(
                    returncode=returncode, stdout="", stderr="")):
            return worker.run_post_draft_qc(self.draft, self.report_dir)

    def wrapped_evidence(self, payload, *, qc_status="PASS", contract_status="VALIDATED"):
        payload = dict(payload)
        payload.setdefault("draft", str(self.draft))
        payload.setdefault("source_bindings", source_bindings_for(Path(payload["draft"])))
        fingerprint = pe.visual_qc_contract.draft_metadata_fingerprint(Path(payload["draft"]))
        payload.setdefault("draft_metadata_sha256", fingerprint)
        payload.setdefault("draft_metadata_sha256_final", payload["draft_metadata_sha256"])
        source = self.write_report(payload)
        wrapper = {"task_id": self.contract["task_id"], "run_id": self.contract["run_id"],
                   "contract_hash": self.contract["contract_hash"],
                   "output_sha256": pe.output_identity([self.draft])["sha256"],
                   "status": "PASS", "qc_status": qc_status, "contract_status": contract_status,
                   "response_version": pe.visual_qc_contract.RESPONSE_VERSION,
                   "reports": [{"path": str(source), "sha256": pe.file_hash(source)}]}
        path = self.root / "qc-evidence.json"
        path.write_text(json.dumps(wrapper), encoding="utf-8")
        return path

    def evaluate(self, path, **additional):
        return pe.evaluate(self.contract, self.task, self.runtime, [self.draft], {"VISUAL-QC": path, **additional})

    @staticmethod
    def criterion(receipt):
        return next(row["status"] for row in receipt["criteria"] if row["id"] == "VISUAL-QC")

    def package_evidence(self, qc_path):
        binding = {key: pe.read(qc_path)[key] for key in
                   ("task_id", "run_id", "contract_hash", "output_sha256")}
        package = self.root / "package-proof.json"
        package.write_text(json.dumps({**binding, "status": "PASS", "files_verified": True}))
        return {"DRAFT-PACKAGE": package}

    def test_worker_accepts_complete_contract_and_blocks_legacy_pass(self):
        self.assertEqual(self.run_worker(qc_report())["status"], "PASS")
        legacy = self.run_worker({"version": "chatcut-visual-qc-v2", "status": "PASS"})
        self.assertEqual((legacy["status"], legacy["rc"]), ("UNCERTIFIED", 2))

    def test_qc_stage_has_independent_900_second_budget_and_cleanup_margin(self):
        script = self.root / "qc.py"
        script.touch()
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"request_timeout": 360, "qc_stage_timeout": 900,
                                      "slot_dir": str(self.root / "slots")}))
        with mock.patch.object(worker, "POST_DRAFT_QC", script), \
                mock.patch.object(worker, "SKILL", self.root), \
                mock.patch.dict("os.environ", {"JY_CONCURRENCY_POLICY_FILE": str(policy),
                    "JY_TASK_DEADLINE_TS": "", "JY_TASK_DEADLINE_EPOCH": ""}), \
                mock.patch.object(worker.subprocess, "run", side_effect=worker.subprocess.TimeoutExpired("qc", 930)) as called:
            answer = worker.run_post_draft_qc(self.draft, self.report_dir)
        self.assertEqual(called.call_args.kwargs["timeout"], 930)
        self.assertEqual(called.call_args.kwargs["env"]["JY_POST_QC_TIMEOUT_S"], "360")
        self.assertEqual(answer["resume_stage"], "qc")
        self.assertTrue(self.draft.exists())

    def test_qc_recovery_never_rebuilds_or_synthesizes(self):
        task_dir = self.root / self.task["record_id"]
        report_dir = task_dir / "report" / "draft-a"
        report_dir.mkdir(parents=True)
        (report_dir / "qc_stage.json").write_text(json.dumps({"draft": str(self.draft), "resume_stage": "qc"}))
        with mock.patch.object(worker, "WORK", self.root), \
                mock.patch.object(worker, "DRAFTS", self.drafts), \
                mock.patch.object(worker, "SKIP_SHIP", True), \
                mock.patch.object(worker, "run_post_draft_qc", return_value={"status": "PASS", "output": "qc.json"}) as review, \
                mock.patch.object(worker, "run_build", side_effect=AssertionError("unexpected rebuild")):
            result = worker.resume_task_qc(self.task)
        self.assertEqual(review.call_count, 1)
        self.assertEqual(result["resume_stage"], "delivery")
        self.assertNotEqual(result.get("distribution_status"), "SHIPPED_VERIFIED")

    def test_rebuild_progress_changes_only_with_new_inputs(self):
        manifest = {"material_sources": [str(self.source)], "full_script": "same"}
        first = worker.build_progress_inputs(manifest, None, [])
        self.assertEqual(first, worker.build_progress_inputs({**manifest, "draft_name": "another"}, None, []))
        self.assertNotEqual(first, worker.build_progress_inputs(manifest, None, [1]))
        self.source.write_bytes(b"changed source bytes")
        self.assertNotEqual(first, worker.build_progress_inputs(manifest, None, []))

    def test_preview_qc_resume_keeps_passing_draft_local(self):
        task_dir = self.root / self.task["record_id"]
        report_dir = task_dir / "report" / "draft-a"
        report_dir.mkdir(parents=True)
        (report_dir / "qc_stage.json").write_text(json.dumps({"draft": str(self.draft)}))
        with mock.patch.object(worker, "WORK", self.root), mock.patch.object(worker, "DRAFTS", self.drafts), \
             mock.patch.object(worker, "SKIP_SHIP", False), \
             mock.patch.object(worker, "run_post_draft_qc", return_value={"status": "PASS", "output": "qc.json"}), \
             mock.patch.object(worker, "finalize_closed_loop") as delivery:
            result = worker.resume_task_qc({**self.task, "delivery_mode": "preview"})
        self.assertEqual((result["status"], result["distribution_status"], result["resume_stage"]),
                         ("PREVIEW_READY", "LOCAL_ONLY", None))
        delivery.assert_not_called()

    def test_qc_resume_keeps_missing_requested_drafts_partial(self):
        task_dir = self.root / self.task["record_id"]
        report_dir = task_dir / "report" / "draft-a"
        report_dir.mkdir(parents=True)
        (report_dir / "qc_stage.json").write_text(json.dumps({"draft": str(self.draft)}))
        for mode in ("preview", "formal"):
            with self.subTest(mode=mode), mock.patch.object(worker, "WORK", self.root), \
                 mock.patch.object(worker, "DRAFTS", self.drafts), mock.patch.object(worker, "SKIP_SHIP", False), \
                 mock.patch.object(worker, "run_post_draft_qc", return_value={"status": "PASS", "output": "qc.json"}), \
                 mock.patch.object(worker, "finalize_closed_loop") as delivery:
                result = worker.resume_task_qc({**self.task, "delivery_mode": mode, "count": 3})
            self.assertEqual((result["status"], result["resume_stage"], result["missing_draft_count"]),
                             ("PARTIAL", "build", 2))
            delivery.assert_not_called()

    def test_worker_preserves_fail_even_when_issue_also_mentions_timeout(self):
        result = self.run_worker({"status": "FAIL", "issues": ["MISMATCH after provider timeout"]}, returncode=2)
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn("nonblocking_timeout", result)

    def test_failed_child_cannot_reuse_a_previous_pass(self):
        answer = self.run_worker(qc_report(), returncode=2)
        self.assertEqual(answer["status"], "UNCERTIFIED")
        self.assertNotEqual(answer["rc"], 0)

    def test_worker_rechecks_rows_even_when_marker_claims_validated(self):
        payload = qc_report()
        payload["model_review"][0]["confidence"] = 0
        self.assertEqual(self.run_worker(payload)["status"], "UNCERTIFIED")

    def test_eval_checks_hashed_report_content_instead_of_wrapper_pass(self):
        self.assertEqual(self.criterion(self.evaluate(self.wrapped_evidence(qc_report()))), "PASS")
        self.assertEqual(self.criterion(self.evaluate(self.wrapped_evidence({"status": "PASS"}))), "UNCERTIFIED")
        payload = qc_report()
        payload["model_review"][0]["observed"] = ""
        self.assertEqual(self.criterion(self.evaluate(self.wrapped_evidence(payload))), "UNCERTIFIED")

    def test_eval_preserves_hash_binding(self):
        path = self.wrapped_evidence(qc_report())
        self.write_report({"status": "PASS"})
        receipt = self.evaluate(path)
        self.assertEqual(self.criterion(receipt), "UNCERTIFIED")
        self.assertTrue(any("TRANSITIVE_EVIDENCE_HASH_MISMATCH" in value for value in receipt["blocking_reasons"]))

    def test_eval_requires_contract_marker_and_preserves_explicit_mismatch(self):
        self.assertEqual(self.criterion(self.evaluate(self.wrapped_evidence(qc_report(), contract_status="UNCERTIFIED"))), "UNCERTIFIED")
        self.assertEqual(self.criterion(self.evaluate(self.wrapped_evidence({"status": "FAIL"}))), "FAIL")

    def test_worker_aggregation_does_not_upgrade_old_report(self):
        path = self.write_report({"status": "PASS"})
        with mock.patch.object(worker, "DRAFTS", self.drafts), \
                mock.patch.object(worker, "validate_draft_package", return_value={"ok": True}), \
                mock.patch.object(pe, "runtime_identity", return_value=self.runtime):
            result = worker.evaluate_task(self.root / "task", self.task, [self.draft.name],
                [{"draft": self.draft.name, "report": str(path)}], self.contract)
        self.assertEqual(self.criterion(result), "UNCERTIFIED")
        qc_path = next((self.root / "task" / "eval" / self.contract["run_id"]).glob("qc-*.json"))
        aggregated = pe.read(qc_path)
        self.assertEqual((aggregated["qc_status"], aggregated["contract_status"]), ("UNCERTIFIED", "UNCERTIFIED"))

    def test_worker_aggregation_accepts_complete_validated_report(self):
        path = self.write_report(qc_report(draft=self.draft))
        with mock.patch.object(worker, "DRAFTS", self.drafts), \
                mock.patch.object(worker, "validate_draft_package", return_value={"ok": True}), \
                mock.patch.object(pe, "runtime_identity", return_value=self.runtime):
            result = worker.evaluate_task(self.root / "task", self.task, [self.draft.name],
                [{"draft": self.draft.name, "report": str(path)}], self.contract)
        self.assertEqual(self.criterion(result), "PASS")

    def test_worker_rejects_valid_qc_report_for_another_draft(self):
        other = self.drafts / "draft-b"
        other.mkdir()
        (other / "draft_info.json").write_text(json.dumps({"id": "draft-id-b", "tracks": []}))
        path = self.write_report(qc_report(draft=other))
        with mock.patch.object(worker, "DRAFTS", self.drafts), \
                mock.patch.object(worker, "validate_draft_package", return_value={"ok": True}), \
                mock.patch.object(pe, "runtime_identity", return_value=self.runtime):
            result = worker.evaluate_task(self.root / "task", self.task, [self.draft.name],
                [{"draft": self.draft.name, "report": str(path)}], self.contract)
        self.assertEqual(self.criterion(result), "UNCERTIFIED")

    def test_worker_rejects_duplicate_or_missing_report_rows(self):
        path = self.write_report(qc_report(draft=self.draft))
        with mock.patch.object(worker, "DRAFTS", self.drafts), \
                mock.patch.object(worker, "validate_draft_package", return_value={"ok": True}), \
                mock.patch.object(pe, "runtime_identity", return_value=self.runtime):
            duplicated = worker.evaluate_task(self.root / "dup", self.task, [self.draft.name],
                [{"draft": self.draft.name, "report": str(path)},
                 {"draft": self.draft.name, "report": str(path)}], self.contract)
            missing = worker.evaluate_task(self.root / "missing", self.task, [self.draft.name], [], self.contract)
        self.assertEqual(self.criterion(duplicated), "UNCERTIFIED")
        self.assertEqual(self.criterion(missing), "UNCERTIFIED")

    def test_eval_rejects_report_bound_to_different_draft(self):
        other = self.drafts / "draft-b"
        other.mkdir()
        (other / "draft_info.json").write_text(json.dumps({"id": "draft-id-b", "tracks": []}))
        self.assertEqual(self.criterion(self.evaluate(
            self.wrapped_evidence(qc_report(draft=other)))), "UNCERTIFIED")

    def test_eval_rejects_same_path_after_timeline_metadata_changes(self):
        path = self.wrapped_evidence(qc_report(draft=self.draft))
        (self.draft / "draft_info.json").write_text(
            json.dumps({"id": "draft-id-a", "tracks": [], "duration": 123}), encoding="utf-8")
        proof = pe.read(path)
        proof["output_sha256"] = pe.output_identity([self.draft])["sha256"]
        path.write_text(json.dumps(proof), encoding="utf-8")
        self.assertEqual(self.criterion(self.evaluate(path)), "UNCERTIFIED")

    def test_pass_without_metadata_fingerprint_is_uncertified(self):
        payload = qc_report(draft=self.draft)
        payload.pop("draft_metadata_sha256")
        payload.pop("draft_metadata_sha256_final")
        self.assertEqual(pe.visual_qc_contract.report_status(payload), "UNCERTIFIED")

    def test_complete_eval_requires_strict_formal_qc(self):
        for change in ("formal", "degraded", "preview"):
            with self.subTest(change=change):
                payload = qc_report("PASS_WITH_DEGRADED" if change == "degraded" else "PASS", self.draft)
                payload["delivery_mode"] = "preview" if change == "preview" else "formal"
                qc_path = self.wrapped_evidence(payload,
                    qc_status="PASS_WITH_DEGRADED" if change == "degraded" else "PASS")
                receipt = self.evaluate(qc_path, **self.package_evidence(qc_path))
                expected = "PASS" if change == "formal" else "UNCERTIFIED"
                self.assertEqual(self.criterion(receipt), expected)
                self.assertEqual(receipt["status"], expected)
                self.assertEqual(receipt["finished_output_acceptance"],
                                 "ACCEPTED" if change == "formal" else "UNVERIFIED")
                self.assertTrue(pe.verify_receipt(receipt, self.contract, self.task, self.runtime, [self.draft]))

    def test_missing_package_proof_never_receives_accepted_label(self):
        qc_path = self.wrapped_evidence(qc_report("PASS", self.draft), qc_status="PASS")
        receipt = self.evaluate(qc_path)
        self.assertEqual(receipt["status"], "UNCERTIFIED")
        self.assertEqual(receipt["finished_output_acceptance"], "UNVERIFIED")

    def test_missing_source_binding_is_uncertified_even_with_valid_metadata(self):
        payload = qc_report(draft=self.draft)
        payload.pop("source_bindings")
        self.assertEqual(pe.visual_qc_contract.report_status(payload), "UNCERTIFIED")
        self.assertFalse(pe.visual_qc_contract.report_matches_draft(payload, self.draft))

    def test_source_bindings_must_cover_every_referenced_video(self):
        payload = qc_report(draft=self.draft)
        first = Path(payload["source_bindings"][0]["path"])
        second = self.draft / "media" / "second.mp4"
        second.write_bytes(b"second actually referenced source")
        info = {"id": "draft-id-a", "materials": {"videos": [
            {"id": "first", "path": str(first)}, {"id": "second", "path": str(second)}]},
            "tracks": [{"type": "video", "segments": [{"material_id": "first"}, {"material_id": "second"}]}]}
        (self.draft / "draft_info.json").write_text(json.dumps(info), encoding="utf-8")
        fingerprint = pe.visual_qc_contract.draft_metadata_fingerprint(self.draft)
        payload.update(draft_metadata_sha256=fingerprint, draft_metadata_sha256_final=fingerprint)
        self.assertFalse(pe.visual_qc_contract.report_matches_draft(payload, self.draft))
        stat = second.stat()
        payload["source_bindings"].append({"path": str(second), "identity": {
            "path": str(second.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}})
        self.assertTrue(pe.visual_qc_contract.report_matches_draft(payload, self.draft))
        second.write_bytes(b"changed second source")
        self.assertFalse(pe.visual_qc_contract.report_matches_draft(payload, self.draft))

    def test_unvalidated_qc_cannot_become_accepted_degraded(self):
        qc_path = self.wrapped_evidence({"status": "PASS_WITH_DEGRADED"}, qc_status="PASS_WITH_DEGRADED")
        receipt = self.evaluate(qc_path, **self.package_evidence(qc_path))
        self.assertEqual(self.criterion(receipt), "UNCERTIFIED")
        self.assertEqual(receipt["finished_output_acceptance"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
