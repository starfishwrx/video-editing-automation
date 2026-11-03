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


    def test_unresolved_voice_does_not_start_material_prep(self):
        prepared = []
        result = dispatch.dispatch(
            [{"record_id": "bad-voice", "material_dir": "pool", "voice": "不存在的音色XYZ"}],
            work=self.root, policy=self.policy,
            preparer=lambda *args: prepared.append("prepared") or "shared.json",
            runner=lambda task, stage, env, work: {"record_id": task["record_id"]})
        self.assertEqual(prepared, [])
        self.assertEqual(result[0]["stage"], "voice")
        self.assertEqual(result[0]["status"], "BLOCKED")
        self.assertTrue(result[0]["error"])

    def test_bad_voice_inputs_are_isolated_and_corrected_task_can_resume(self):
        invalid = [
            {"voice": "憨熊3.0"}, {"voice": "憨熊0.3"}, {"voice": "1.2倍速"},
            {"voice": "憨熊", "voice_tempo": 3.0},
            {"voice": "憨熊", "voice_tempo": "nan"},
            {"voice": "憨熊", "voice_tempo": True},
            {"voice": "憨熊", "voice_tempo_status": "invalid"},
            {"voice": "", "voice_original": "憨熊3.0"},
            {"voice": "", "voice_tempo": "fast"},
        ]
        tasks = [dict(case, record_id=f"bad-{i}", material_dir="same")
                 for i, case in enumerate(invalid)]
        tasks.append({"record_id": "good", "material_dir": "same", "voice": "憨熊1.2"})
        prepared, ran = [], []
        def prepare(task, *args):
            prepared.append(task["record_id"])
            return "shared.json"
        def run(task, *args):
            ran.append(task["record_id"])
            return {"record_id": task["record_id"], "status": "OK"}
        rows = dispatch.dispatch(tasks, work=self.root, policy=self.policy,
                                 preparer=prepare, runner=run)
        self.assertEqual(prepared, ["good"])
        self.assertEqual(ran, ["good"])
        blocked = [row for row in rows if row.get("status") == "BLOCKED"]
        self.assertEqual(len(blocked), len(invalid))
        self.assertTrue(all(row["stage"] == "voice" and row["error"] and
                            row["formal_deliveries"] == 0 for row in blocked))
        runs = [json.loads(path.read_text(encoding="utf-8")) for path in
                (self.root / "dispatch_runs").glob("*.json")]
        self.assertEqual(len(runs), 1)
        self.assertIsNotNone(runs[0]["finished_at"])
        self.assertEqual(runs[0]["results"], rows)
        progress = [json.loads(line) for line in
                    (self.root / "batch_dispatch_progress.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(progress), len(rows))
        fixed = dict(tasks[0], voice="憨熊0.5")
        recovered = dispatch.dispatch([fixed], work=self.root, policy=self.policy,
                                      resume_stage="build", preparer=prepare, runner=run)
        self.assertEqual(recovered[-1]["status"], "OK")
        self.assertEqual(ran, ["good", "bad-0"])

    def test_voice_admission_matches_worker_normalization_and_tempo_boundaries(self):
        valid = [
            {"voice": "憨熊0.5"}, {"voice": "憨熊2.0"},
            {"voice": "憨熊1.2", "voice_tempo": 1.0},
            {"voice": "", "voice_original": "憨熊1.2"},
            {"voice": "", "voice_tempo": 1.2}, {}, {"voice": "default"},
        ]
        for task in valid:
            original = dict(task)
            with self.subTest(task=task):
                self.assertIsNone(dispatch._voice_error(task))
                self.assertEqual(task, original)
        with patch.dict(dispatch.os.environ, {"JY_OPERATOR_VOICE": "憨熊3.0"}):
            self.assertIsNotNone(dispatch._voice_error({"voice": ""}))
            self.assertIsNone(dispatch._voice_error({"voice": "憨熊1.2"}))
        with patch.dict(dispatch.os.environ, {"JY_OPERATOR_VOICE": "憨熊1.2"}):
            self.assertIsNone(dispatch._voice_error({"voice": ""}))

    def test_missing_music_blocks_auto_bgm_but_local_path_and_qc_resume_continue(self):
        from orchestrator.audio import library_resources
        catalog = library_resources.CATALOGS_ROOT
        with patch.object(library_resources, "CATALOGS_ROOT", self.root / "catalogs"):
            library_resources.CATALOGS_ROOT.mkdir()
            import shutil
            for name in ("jianying_voice_catalog.csv", "tts_speakers.csv"):
                shutil.copyfile(catalog / name, library_resources.CATALOGS_ROOT / name)
            prepared, ran = [], []
            tasks = [{"record_id": "auto", "voice": "憨熊"},
                     {"record_id": "local", "voice": "憨熊", "bgm": "C:/fixture.wav"}]
            rows = dispatch.dispatch(tasks, work=self.root, policy=self.policy,
                preparer=lambda task, *args: prepared.append(task["record_id"]) or "shared.json",
                runner=lambda task, *args: ran.append(task["record_id"]) or {"record_id": task["record_id"]})
            self.assertEqual(prepared, ["local"])
            self.assertEqual(ran, ["local"])
            self.assertEqual(rows[0]["status"], "BLOCKED")
            self.assertIn("RUNTIME_CATALOG_MISSING:cloud_music_library.csv", rows[0]["error"])
        with patch.object(library_resources, "CATALOGS_ROOT", self.root / "absent"):
            rows = dispatch.dispatch([{"record_id": "qc", "voice": "憨熊3.0"}],
                work=self.root, policy=self.policy, resume_stage="qc",
                preparer=lambda *args: self.fail("QC must not prepare material"),
                runner=lambda task, *args: {"record_id": task["record_id"], "status": "OK"})
            self.assertEqual(rows[0]["status"], "OK")

    def test_missing_voice_catalog_blocks_even_empty_or_preset_voice_before_preparation(self):
        from orchestrator.audio import library_resources
        for voice in ("", "male_energetic", "真人播客女"):
            with self.subTest(voice=voice), patch.object(library_resources, "CATALOGS_ROOT", self.root / "absent"):
                rows = dispatch.dispatch([{"record_id": "missing", "voice": voice}],
                    work=self.root, policy=self.policy,
                    preparer=lambda *args: self.fail("catalog failure must precede analysis"),
                    runner=lambda *args: self.fail("catalog failure must precede build"))
                self.assertEqual(rows[0]["status"], "BLOCKED")
                self.assertIn("RUNTIME_CATALOG_MISSING", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
