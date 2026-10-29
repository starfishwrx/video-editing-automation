from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import batch_dispatch as dispatch
from orchestrator.runtime_policy import RuntimePolicy
from orchestrator.file_safety import file_lock


class BatchDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = RuntimePolicy(task_workers=1, prep_workers=2, slot_dir=str(self.root / "slots"))

    def test_cold_waiters_do_not_take_ready_worker_and_product_is_prepared_once(self):
        released = threading.Event()
        prepared, ran = [], []
        def prepare(task, directory, env):
            prepared.append(task["material_dir"])
            if task["material_dir"] == "cold":
                self.assertTrue(released.wait(5), "ready task was blocked behind cold material waiters")
            return str(directory / "shot_candidates.json")
        def run(task, stage, env, work):
            self.assertIn("JY_SHARED_ANALYSIS_FILE", env)
            self.assertEqual(env["JY_VISION_TEXT_PASS"], "1")
            self.assertEqual(env["JY_TASK_DEADLINE_TS"], env["JY_TASK_DEADLINE_EPOCH"])
            ran.append(task["record_id"])
            released.set()
            return {"record_id": task["record_id"], "formal_deliveries": 0}
        tasks = [{"record_id": f"cold-{i}", "material_dir": "cold"} for i in range(4)]
        tasks += [{"record_id": "ready", "material_dir": "hot"}]
        result = dispatch.dispatch(tasks, work=self.root, policy=self.policy, preparer=prepare, runner=run)
        self.assertEqual(ran[0], "ready")
        self.assertEqual(sorted(prepared), ["cold", "hot"])
        self.assertEqual(len([row for row in result if row.get("stage") != "material"]), 5)
        self.assertEqual(len([row for row in result if row.get("stage") == "material"]), 2)

    def test_product_group_does_not_share_different_material_or_sku(self):
        task = {"product": "same", "material_sources": ["one.mp4"], "sku": "a"}
        self.assertNotEqual(dispatch.product_key(task), dispatch.product_key({**task, "material_sources": ["two.mp4"]}))
        self.assertNotEqual(dispatch.product_key(task), dispatch.product_key({**task, "sku": "b"}))

    def test_qc_priority_then_fifo_and_duplicate_record_is_once(self):
        ran = []
        for rid in ("q1", "q2"):
            directory = self.root / rid
            directory.mkdir()
            (directory / "done.json").write_text(json.dumps({"drafts": [rid], "resume_stage": "qc"}))
        def run(task, stage, env, work):
            ran.append((task["record_id"], stage))
            return {"record_id": task["record_id"]}
        tasks = [{"record_id": rid, "material_dir": "same"} for rid in ("b1", "q1", "q2", "b2", "b1")]
        dispatch.dispatch(tasks, work=self.root, policy=self.policy,
                          preparer=lambda *args: "shared.json", runner=run)
        self.assertEqual(ran, [("q1", "qc"), ("q2", "qc"), ("b1", "build"), ("b2", "build")])

    def test_second_entry_is_durable_and_owner_absorbs_submission(self):
        with file_lock(Path(self.policy.slot_dir) / "batch_dispatch", timeout=0):
            result = dispatch.dispatch([{"record_id": "queued"}], work=self.root, policy=self.policy)
        self.assertEqual(result[0]["status"], "SUBMITTED")
        self.assertTrue(Path(result[0]["submission"]).is_file())
        ran = []
        dispatch.dispatch([], work=self.root, policy=self.policy,
            preparer=lambda *args: "shared.json",
            runner=lambda task, stage, env, work: ran.append(task["record_id"]) or {"record_id": task["record_id"]})
        self.assertEqual(ran, ["queued"])
        self.assertFalse(Path(result[0]["submission"]).exists())

    def test_dry_run_is_read_only_and_material_resume_does_not_build(self):
        work = self.root / "new"
        task = {"record_id": "a"}
        dispatch.dispatch([task], work=work, policy=self.policy, dry_run=True)
        self.assertFalse(work.exists())
        result = dispatch.dispatch([task], work=work, policy=self.policy, resume_stage="material",
            preparer=lambda *args: "shared.json",
            runner=lambda *args: self.fail("material resume invoked build"))
        self.assertEqual(result[0]["status"], "READY")

    def test_wait_uses_short_timeout_to_allow_periodic_polling(self):
        # Verify that dispatch.wait is called with a timeout to enable periodic polling
        called = {"timeout_values": []}

        real_wait = dispatch.wait

        def wrapped_wait(fs, timeout=None, return_when=None):
            called["timeout_values"].append(timeout)
            # Defer to the real wait but respect the provided timeout
            return real_wait(fs, timeout=timeout, return_when=return_when)

        def preparer(task, directory, env):
            path = directory / "shot_candidates.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            return str(path)

        def runner(task, stage, env, work):
            return {"record_id": task["record_id"], "formal_deliveries": 0}

        old = dispatch.wait
        try:
            dispatch.wait = wrapped_wait  # monkeypatch module-level imported wait
            dispatch.dispatch([{"record_id": "one", "material_dir": "m"}],
                              work=self.root, policy=self.policy,
                              preparer=preparer, runner=runner)
        finally:
            dispatch.wait = old
        self.assertTrue(any((t is not None and t > 0) for t in called["timeout_values"]),
                        f"expected wait to be called with a positive timeout; seen={called['timeout_values']}")

    def test_stage_and_wait_timings_are_measured_and_mp4_is_unknown(self):
        rows = dispatch.dispatch([{"record_id": "timed"}], work=self.root, policy=self.policy,
            preparer=lambda *args: "shared.json",
            runner=lambda *args: {"record_id": "timed", "formal_deliveries": 2})
        material, task = rows
        self.assertEqual(material["timing_scope"], "product_preparation")
        self.assertEqual(task["stage"], "build")
        self.assertGreaterEqual(task["elapsed_s"], 0)
        self.assertAlmostEqual(task["queue_wait_s"],
                               task["material_wait_s"] + task["worker_queue_wait_s"])
        self.assertEqual(task["verified_draft_deliveries"], 2)
        self.assertIsNone(task["final_mp4_deliveries"])
        self.assertEqual(task["queue_wait_origin"], "supervisor_admission")

    def test_submission_wait_starts_at_submission_not_supervisor_start(self):
        submitted_at = dispatch.time.time() - 30
        with patch.object(dispatch.time, "time", return_value=submitted_at):
            dispatch.submit([{"record_id": "late"}], work=self.root, policy=self.policy,
                            resume_stage="qc")
        rows = dispatch.dispatch([], work=self.root, policy=self.policy,
            preparer=lambda *args: self.fail("QC should not prepare material"),
            runner=lambda *args: {"record_id": "late"})
        self.assertEqual(rows[0]["queue_wait_origin"], "submission")
        self.assertGreaterEqual(rows[0]["queue_wait_s"], 29.9)
        self.assertEqual(rows[0]["material_wait_s"], 0)

    def test_failed_preparation_and_runner_retain_measured_timings(self):
        def fail(*args):
            raise RuntimeError("offline injected failure")
        rows = dispatch.dispatch([{"record_id": "bad-material"}], work=self.root,
            policy=self.policy, preparer=fail, runner=lambda *args: self.fail("must not build"))
        self.assertEqual([row["status"] for row in rows], ["ERROR", "BLOCKED"])
        self.assertEqual(rows[0]["timing_scope"], "product_preparation")
        self.assertGreaterEqual(rows[0]["elapsed_s"], 0)
        rows = dispatch.dispatch([{"record_id": "bad-build"}], work=self.root,
            policy=self.policy, preparer=lambda *args: "shared.json", runner=fail)
        self.assertEqual(rows[-1]["status"], "ERROR")
        self.assertEqual(rows[-1]["stage"], "build")
        self.assertGreaterEqual(rows[-1]["elapsed_s"], 0)
        self.assertEqual(rows[-1]["verified_draft_deliveries"], 0)

    def test_ok_or_preview_flags_without_certified_delivery_never_count(self):
        for status in ("OK", "PREVIEW_READY", "ERROR"):
            self.assertEqual(dispatch.formal_delivery_count({"status": status, "drafts": ["x"]}), 0)
        self.assertEqual(dispatch.formal_delivery_count({"status": "OK", "distribution_status": "SHIPPED_VERIFIED",
            "drafts": ["x"], "qc_reports": [{"draft": "x", "status": "UNCERTIFIED_TIMEOUT"}],
            "distribution_receipts": [{"draft_name": "x", "status": "VERIFIED"}]}), 0)

    def test_qc_resume_preserves_frozen_task_snapshot(self):
        task_dir = self.root / 'r1'
        task_dir.mkdir()
        frozen_task = {'record_id': 'r1', 'script': 'original', 'retry_deferred': True}
        (task_dir / 'task.json').write_text(json.dumps(frozen_task), encoding='utf-8')
        def invoke(args, **kwargs):
            self.assertEqual(json.loads((task_dir / 'task.json').read_text(encoding='utf-8')), frozen_task)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with mock.patch.object(dispatch.subprocess, 'run', side_effect=invoke):
            dispatch.run_task({'record_id': 'r1', 'script': 'changed'}, 'qc',
                              {'JY_TASK_DEADLINE_EPOCH': str(time.time() + 20)}, self.root)
        self.assertEqual(json.loads((task_dir / 'task.json').read_text(encoding='utf-8')), frozen_task)


if __name__ == "__main__":
    unittest.main()
