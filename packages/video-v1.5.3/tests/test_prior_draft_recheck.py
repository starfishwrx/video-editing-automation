import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import batch_worker  # noqa: E402


class PriorDraftRecheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Point drafts root to a temp directory
        os.environ["JY_DRAFT_ROOT"] = str(self.root / "drafts")
        (self.root / "drafts").mkdir(parents=True, exist_ok=True)
        self.task_dir = self.root / "work" / "record-a"
        (self.task_dir / "report" / "report_2").mkdir(parents=True, exist_ok=True)

    def test_indexed_draft_name_is_checked_for_second_and_later_outputs(self):
        base = "deliver"
        index = 2
        # Minimal persisted QC json
        qc_path = self.task_dir / "report" / "report_2" / "draft_visual_qc.json"
        qc_path.write_text(json.dumps({"status": "PASS", "draft": str(self.root / 'drafts' / f'{base}-{index}')}, ensure_ascii=False), encoding="utf-8")
        matched_path_seen = []

        def report_matches_draft(report, draft_path):
            matched_path_seen.append(str(draft_path))
            # Return True only when the draft path matches the indexed name
            return os.path.normcase(str(draft_path)) == os.path.normcase(str(self.root / "drafts" / f"{base}-{index}"))

        with mock.patch.object(batch_worker, "DRAFTS", self.root / "drafts"), \
             mock.patch.object(batch_worker.production_eval.visual_qc_contract, "report_status", return_value="PASS"), \
             mock.patch.object(batch_worker.production_eval.visual_qc_contract, "report_matches_draft", side_effect=report_matches_draft):
            needs = batch_worker.prior_draft_requires_recheck(self.task_dir, index, base, previous={})
        self.assertFalse(needs, f"Expected no recheck when PASS binds to {base}-{index}")
        self.assertTrue(matched_path_seen, "report_matches_draft should be invoked")
        self.assertEqual(os.path.normcase(matched_path_seen[-1]),
                         os.path.normcase(str(self.root / "drafts" / f"{base}-{index}")))


if __name__ == "__main__":
    unittest.main()
