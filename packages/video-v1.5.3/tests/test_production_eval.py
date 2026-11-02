"""Current Windows inputs, package and independent QC are the complete contract."""
import copy
from pathlib import Path
import tempfile
import unittest

from tools.acceptance import evaluation as pe, task_review
from acceptance_fixtures import make_acceptance, write


class WindowsCoreEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="windows-core-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task_dir = self.root / "task"
        self.draft_root = self.root / "drafts"
        self.draft = self.draft_root / "draft-a"
        self.data = make_acceptance(self.task_dir, self.draft_root, [self.draft.name],
            {"record_id": "fixture", "task_id": "fixture-task", "script": "Synthetic script"})
        self.contract = self.data["contract"]
        self.receipt = pe.read(Path(self.data["receipt"]["receipt_path"]))

    def evaluate(self):
        return task_review.evaluate_task(
            self.task_dir, self.data["task"], [self.draft.name], self.data["qc_reports"],
            self.contract, draft_root=self.draft_root, runtime_root=self.data["runtime_root"],
            package_validator=lambda path: task_review.validate_draft_package(
                path, video_extensions=(".mp4", ".mov", ".m4v")), read_json=pe.read)

    def test_four_current_core_criteria_are_sufficient_and_replayable(self):
        self.assertEqual(pe.CRITERIA, ("INPUT-IDENTITY", "RUNTIME-IDENTITY", "DRAFT-PACKAGE", "VISUAL-QC"))
        self.assertEqual(self.receipt["schema_version"], 2)
        self.assertEqual(self.receipt["status"], "PASS")
        self.assertEqual([row["id"] for row in self.receipt["criteria"]], list(pe.CRITERIA))
        self.assertTrue(pe.verify_receipt(self.receipt, self.contract, self.data["task"],
                                         self.data["runtime"], [self.draft]))
        self.assertNotIn("gold_reference", self.contract)
        self.assertNotIn("human_gates", self.contract["eval_contract"])

    def test_retired_optional_gold_argument_does_not_change_contract(self):
        inputs = [Path(row["path"]) for row in self.contract["inputs"]]
        plain = pe.freeze(self.data["task"], "same-run", inputs, self.data["runtime"])
        compatible = pe.freeze(self.data["task"], "same-run", inputs, self.data["runtime"],
                               gold={"retired": "ignored"})
        self.assertEqual(compatible, plain)

    def test_old_contract_schema_and_old_acceptance_set_are_rejected(self):
        for schema, criteria in ((1, list(pe.CRITERIA)), (2, list(pe.CRITERIA) + ["MAC-REVIEW", "GOLD-QUALITY"])):
            with self.subTest(schema=schema, criteria=criteria):
                old = copy.deepcopy(self.contract)
                old["schema_version"] = schema
                old["eval_contract"]["acceptance_criteria"] = [{"id": name} for name in criteria]
                old.pop("contract_hash")
                old["contract_hash"] = "sha256:" + pe.digest(old)
                with self.assertRaises(ValueError):
                    pe.verify_contract(old)
                self.assertFalse(pe.verify_receipt(self.receipt, old, self.data["task"],
                                                   self.data["runtime"], [self.draft]))

    def test_old_receipt_cannot_be_promoted_by_rehashing(self):
        old = copy.deepcopy(self.receipt)
        old["schema_version"] = 1
        old.pop("receipt_sha256")
        old["receipt_sha256"] = pe.digest(old)
        self.assertFalse(pe.verify_receipt(old, self.contract, self.data["task"],
                                           self.data["runtime"], [self.draft]))

    def test_changed_input_or_runtime_invalidates_current_receipt(self):
        changed_runtime = {**self.data["runtime"], "tool_version": "other"}
        self.assertFalse(pe.verify_receipt(self.receipt, self.contract, self.data["task"],
                                           changed_runtime, [self.draft]))
        Path(self.contract["inputs"][0]["path"]).write_bytes(b"changed input")
        result = self.evaluate()
        self.assertEqual(next(row["status"] for row in result["criteria"]
                              if row["id"] == "INPUT-IDENTITY"), "UNCERTIFIED")
        self.assertEqual(result["status"], "UNCERTIFIED")

    def test_preview_and_status_only_qc_never_pass(self):
        report_path = Path(self.data["qc_reports"][0]["report"])
        original = pe.read(report_path)
        for payload in ({**original, "delivery_mode": "preview"}, {"status": "PASS"}):
            with self.subTest(payload=payload):
                write(report_path, payload)
                result = self.evaluate()
                self.assertEqual(result["status"], "UNCERTIFIED")
                self.assertEqual(next(row["status"] for row in result["criteria"]
                                      if row["id"] == "VISUAL-QC"), "UNCERTIFIED")

    def test_output_edits_and_source_edits_reject_stored_acceptance(self):
        media = self.draft / "media" / "qc-source.mp4"
        media.write_bytes(b"changed referenced video")
        self.assertFalse(pe.verify_receipt(self.receipt, self.contract, self.data["task"],
                                           self.data["runtime"], [self.draft]))
        info = pe.read(self.draft / "draft_info.json")
        write(self.draft / "draft_info.json", {**info, "duration": 2000000})
        self.assertEqual(self.evaluate()["status"], "UNCERTIFIED")

    def test_task_validation_never_reads_retired_selection(self):
        path = self.task_dir / "eval" / self.contract["run_id"] / "review-selection-retired.json"
        path.write_bytes(b"invalid retired selection artifact")
        self.assertEqual(self.evaluate()["status"], "PASS")
        self.assertEqual(path.read_bytes(), b"invalid retired selection artifact")

    def test_unsafe_or_duplicate_draft_names_are_rejected(self):
        for names in ([".."], [str(self.draft)], [self.draft.name, self.draft.name], [{}]):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, "TASK_DRAFTS_MISSING_OR_INVALID"):
                task_review.evaluate_task(
                    self.task_dir, self.data["task"], names, self.data["qc_reports"], self.contract,
                    draft_root=self.draft_root, runtime_root=self.data["runtime_root"],
                    package_validator=lambda _: {"ok": True}, read_json=pe.read)


if __name__ == "__main__":
    unittest.main()
