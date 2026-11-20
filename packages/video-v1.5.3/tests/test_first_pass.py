import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools import first_pass


class FirstPassTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = {"task_id": "order", "record_id": "record", "category": "paper", "script": "five packs"}
        self.task["first_pass_cohort_id"] = "this-admission"
        self.token = first_pass.revision(self.task["script"])
        self.batch = first_pass.queue_identity([self.task])
        self.event = {"record_id": "record", "batch_id": self.batch, "run_id": "run", "draft": "take1",
                      "script_sha256": self.token, "draft_metadata_sha256": "a" * 64, "event": "draft_written"}
        self.event["first_pass_cohort_id"] = "this-admission"
        self.audit = {"record": {"first_pass_cohort_id": "this-admission", "batch_id": self.batch, "script_sha256": self.token, "blind": "PASS",
                      "draft_metadata_sha256": "a" * 64, "manual_before_first": False, "manual_before_delivery": False,
                      "delivery_run_id": "run"}}

    def report(self, events=None):
        events = [self.event, {**self.event, "event": "draft_qc", "status": "PASS"}] if events is None else events
        return first_pass.summarize([self.task], self.root, events, self.audit)

    def test_formal_delivery_binds_run_table_and_every_draft(self):
        path = self.root / "record"
        path.mkdir()
        nas = r"\\server\share\take1"
        done = {"record_id": "record", "task_id": "order", "run_id": "run", "terminal_state": "DELIVERED",
                "delivery_mode": "formal", "distribution_status": "VERIFIED", "drafts": ["take1"],
                "qc_reports": [{"draft": "take1", "status": "PASS"}], "nas_parent_folder": nas,
                "distribution_receipts": [{"task_id": "order", "run_id": "run", "draft_name": "take1",
                                           "nas_target": nas, "distribution_verified": True}]}
        table = {"ok": True, "write_acknowledged": True, "readback_verified": True, "status": "TABLE_UPDATE_VERIFIED",
                 "record_id": "record", "task_id": "order", "patch": {"草稿文件": nas}}
        (path / "done.json").write_text(json.dumps(done), encoding="utf-8")
        (path / "table_update_receipt.json").write_text(json.dumps(table), encoding="utf-8")
        counts = self.report()["by_category"]["paper"]
        self.assertEqual((counts["first_business_rate"], counts["unattended_delivery_rate"]), (1, 1))
        self.audit["record"]["manual_before_first"] = True
        self.assertEqual(self.report()["by_category"]["paper"]["unattended_delivery"], 0)
        self.audit["record"].update(manual_before_first=False, delivery_run_id="other")
        self.assertEqual(self.report()["by_category"]["paper"]["unattended_delivery"], 0)
        table["patch"]["草稿文件"] = "other/take1"
        (path / "table_update_receipt.json").write_text(json.dumps(table), encoding="utf-8")
        self.assertEqual(self.report()["by_category"]["paper"]["formal_delivery"], 0)

    def test_grouped_versions_link_original_shipments_to_table_parent(self):
        from tools.workflow.closed_loop import digest
        path = self.root / "record"
        path.mkdir()
        names = ["first", "second"]
        sources = ["nas/original/" + n for n in names]
        parent = "nas/final"
        body = {"parent_folder": parent, "versions": [parent + "/版本1", parent + "/版本2"], "source_links": sources,
                "version_identities": [{"sha256": n * 64, "file_count": 3} for n in ("a", "b")]}
        archive = {**body, "receipt_hash": digest(body), "record_id": "record", "verified": True, "self_contained_versions": True}
        done = {"record_id": "record", "task_id": "order", "run_id": "run", "terminal_state": "DELIVERED",
                "delivery_mode": "formal", "distribution_status": "GROUPED_VERIFIED", "drafts": names,
                "qc_reports": [{"draft": n, "status": "PASS"} for n in names], "nas_parent_folder": parent,
                "distribution_receipts": [{"task_id": "order", "run_id": "run", "draft_name": n,
                    "nas_target": target, "distribution_verified": True} for n, target in zip(names, sources)]}
        table = {"ok": True, "write_acknowledged": True, "readback_verified": True, "status": "TABLE_UPDATE_VERIFIED",
                 "record_id": "record", "task_id": "order", "patch": {"草稿文件": parent}}
        for name, value in [("done", done), ("table_update_receipt", table), ("distribution_receipt", archive)]:
            (path / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.report()["by_category"]["paper"]["formal_delivery"], 1)
        archive["source_links"] = ["unrelated", sources[1]]
        (path / "distribution_receipt.json").write_text(json.dumps(archive), encoding="utf-8")
        self.assertEqual(self.report()["by_category"]["paper"]["formal_delivery"], 0)

    def test_later_draft_cannot_replace_first_or_count_missing_audit_as_pass(self):
        missing = {**self.event, "draft": "earliest", "draft_metadata_sha256": None}
        counts = self.report([missing, self.event, {**self.event, "event": "draft_qc", "status": "PASS"}])["by_category"]["paper"]
        self.assertEqual((counts["first_business_pass"], counts["first_machine_pass"], counts["blind_pending"]), (0, 0, 1))
        self.assertEqual(self.report([])["by_category"]["paper"]["enrolled"], 1)
        old = [{**self.event, "first_pass_cohort_id": "different-admission"}]
        self.assertIsNone(self.report(old)["orders"][0]["first_qc"])
        self.assertEqual(self.report(old)["by_category"]["paper"]["first_business_pass"], 0)
        self.audit["record"]["blind"] = "FAIL"
        self.assertEqual(self.report()["by_category"]["paper"]["false_release"], 1)
        self.audit["record"]["script_sha256"] = "stale"
        self.assertEqual(self.report()["by_category"]["paper"]["blind_pending"], 1)

    def test_automatic_resume_keeps_original_cohort_and_edited_order_gets_new_admission(self):
        done = dict(record_id="record", task_id="order", run_id="old-run", first_pass_cohort_id="first",
                    first_pass_script_sha256=self.token, terminal_state="QC_PENDING")
        self.assertEqual(first_pass.recovered_cohort(self.task, done), "first")
        self.assertIsNone(first_pass.recovered_cohort({**self.task, "script": "new copy"}, done))
        self.assertIsNone(first_pass.recovered_cohort(self.task, {**done, "terminal_state": "DELIVERED"}))

    def test_revisions_do_not_duplicate_enrollment(self):
        queue = [self.task, {**self.task, "script": "revised"}]
        summary = first_pass.summarize(queue, self.root, [], {})
        self.assertEqual(summary["by_category"]["paper"]["enrolled"], 1)
        self.assertEqual(summary["orders"][0]["later_script_revisions"], [first_pass.revision("revised")])
