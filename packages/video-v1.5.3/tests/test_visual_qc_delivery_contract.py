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
import batch_worker as worker
from tools import production_eval as pe


def qc_report(status="PASS", draft=None):
    degraded = status == "PASS_WITH_DEGRADED"
    result = {"version": pe.visual_qc_contract.REPORT_VERSION, "status": status,
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
                     "script": "合成测试脚本"}
        self.runtime = {"tool_version": "1.5.6", "visual_policy_version": "test",
                        "package_sha256": "a" * 64, "visual_policy_sha256": "b" * 64}
        self.contract = pe.freeze(self.task, "qc-test-run", [self.source], self.runtime,
                                  pe.read(ROOT / "gold-reference.json"))

    def write_report(self, payload):
        target = self.report_dir / "draft_visual_qc.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        return target

    def run_worker(self, payload, *, returncode=0):
        payload = dict(payload)
        payload.setdefault("draft", str(self.draft))
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

    def test_worker_accepts_complete_contract_and_blocks_legacy_pass(self):
        self.assertEqual(self.run_worker(qc_report())["status"], "PASS")
        legacy = self.run_worker({"version": "chatcut-visual-qc-v2", "status": "PASS"})
        self.assertEqual((legacy["status"], legacy["rc"]), ("UNCERTIFIED", 2))

    def test_worker_preserves_fail_even_when_issue_also_mentions_timeout(self):
        result = self.run_worker({"status": "FAIL", "issues": ["MISMATCH after provider timeout"]}, returncode=2)
        self.assertEqual(result["status"], "FAIL")
        self.assertNotIn("nonblocking_timeout", result)

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

    def test_unvalidated_qc_cannot_become_accepted_degraded(self):
        qc_path = self.wrapped_evidence({"status": "PASS_WITH_DEGRADED"}, qc_status="PASS_WITH_DEGRADED")
        binding = {key: pe.read(qc_path)[key] for key in
                   ("task_id", "run_id", "contract_hash", "output_sha256")}
        package = self.root / "package-proof.json"
        package.write_text(json.dumps({**binding, "status": "PASS", "files_verified": True}))
        screenshot = self.root / "synthetic-mac-proof.png"
        screenshot.write_bytes(b"synthetic review evidence")
        mac = self.root / "mac-proof.json"
        mac.write_text(json.dumps({**binding, "status": "PASS", "reviewed_by": "synthetic tester",
            "approved": True, "review_surface": "mac_jianying", "editable_project_opened": True,
            "draft_ids": ["draft-id-a"], "reports": [{"path": str(screenshot), "sha256": pe.file_hash(screenshot)}]}))
        receipt = self.evaluate(qc_path, **{"DRAFT-PACKAGE": package, "MAC-REVIEW": mac})
        self.assertEqual(self.criterion(receipt), "UNCERTIFIED")
        self.assertEqual(receipt["finished_output_acceptance"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
