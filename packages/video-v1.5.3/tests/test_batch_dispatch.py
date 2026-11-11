from dataclasses import replace
import json
from pathlib import Path
import sys
import subprocess
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
        self.preflight = patch.object(dispatch, "_production_preflight", return_value={"ready": True, "runtime": {}})
        self.preflight.start()
        self.addCleanup(self.preflight.stop)

    def test_frozen_single_task_recovery_does_not_schedule_product_preparation(self):
        task = {"record_id": "r", "title": "old"}
        rd = self.root / "r" / "report" / "old"
        rd.mkdir(parents=True)
        (rd / "last_result.json").write_text(json.dumps({"retryable": True, "resume_stage": "draft_write"}))
        prepare = mock.Mock(return_value="analysis.json")
        run = mock.Mock(return_value={"status": "OK"})
        rows = dispatch.dispatch([task], work=self.root, policy=self.policy, preparer=prepare, runner=run)
        self.assertEqual([r["stage"] for r in rows], ["build"])
        prepare.assert_not_called()
        run.assert_called_once()
        self.assertFalse(dispatch._resumes_frozen_build({**task, "force_rebuild": True}, self.root))
        self.assertFalse(dispatch._resumes_frozen_build({**task, "count": 2}, self.root))
        rows = dispatch.dispatch([task], work=self.root, policy=self.policy, preparer=prepare,
                                 runner=run, resume_stage="material")
        prepare.assert_called_once()
        self.assertTrue(all(r["stage"] == "material" for r in rows))
        import batch_worker
        (rd.parent / "report_2").mkdir()
        (rd.parent / "report_2" / "last_result.json").write_text(
            json.dumps({"retryable": True, "resume_stage": "draft_write"}))
        (rd / "last_result.json").write_text("{}")
        (self.root / "r" / "done.json").write_text(json.dumps({"drafts": ["accepted"]}))
        (self.root / "accepted").mkdir()
        with mock.patch.object(batch_worker, "DRAFTS", self.root), \
                mock.patch.object(batch_worker, "prior_draft_requires_recheck", return_value=False):
            self.assertTrue(dispatch._resumes_frozen_build({**task, "count": 2}, self.root))


    def test_task_speed_cap_is_forwarded_from_top_level_and_nested_manifest(self):
        from tools.queueing.contracts import task_runtime_overrides
        self.assertEqual(task_runtime_overrides({'max_absorb_demo_speed': 1.5,
            'manifest': {'max_absorb_demo_speed': 2}})['max_absorb_demo_speed'], 1.5)
        self.assertEqual(task_runtime_overrides({'manifest': {'max_absorb_demo_speed': 2}})
                         ['max_absorb_demo_speed'], 2)
        self.assertNotIn('max_absorb_demo_speed', task_runtime_overrides({}))
        self.assertEqual(task_runtime_overrides({'manifest': {'max_source_reuse': 1}})
                         ['max_source_reuse'], 1)

    def test_new_note_input_has_versioned_numbering_and_old_retry_is_unchanged(self):
        from tools.queueing.contracts import task_shot_note_overrides
        task = {'画面备注': '第2句：展示包装'}
        self.assertEqual(task_shot_note_overrides(task, remark_field='画面备注', new_task=True),
            {'shot_notes': '第2句：展示包装', 'shot_notes_index_basis': 'original_sentence'})
        self.assertEqual(task_shot_note_overrides(task, remark_field='画面备注', new_task=False), {})
        self.assertEqual(task_shot_note_overrides({'manifest': {'自定义备注': '说明'}},
            remark_field='自定义备注', new_task=True)['shot_notes'], '说明')

    def test_task_writer_preflight_blocks_before_any_material_preparation(self):
        self.preflight.stop()
        from orchestrator.application import cli
        from orchestrator.runtime import platform_env, task_log
        prepared = []
        with patch.object(platform_env, "environment_report", return_value={"ready": True}), \
             patch.object(cli, "_production_environment_report", return_value={
                 "ready": False, "reason": "HEADLESS_SUBTITLE_FONT_REQUIRED"}) as check, \
             patch.object(task_log, "runtime_facts", return_value={"pipeline_version": "test"}):
            rows = dispatch.dispatch([{"record_id": "r1", "draft_backend": "headless",
                                       "subtitle_font_path": "missing-font"}],
                work=self.root, policy=self.policy,
                preparer=lambda *args: prepared.append(True),
                runner=lambda *args: self.fail("worker must not run"))
        self.assertEqual(prepared, [])
        self.assertEqual(rows[0]["stage"], "preflight")
        self.assertIn("HEADLESS_SUBTITLE_FONT_REQUIRED", rows[0]["error"])
        self.assertEqual(check.call_args.args[1]["subtitle_font_path"], "missing-font")

    def test_qc_resume_preflights_the_frozen_task_before_the_runner(self):
        task_dir = self.root / 'qc'
        task_dir.mkdir()
        (task_dir / 'task.json').write_text(json.dumps({
            'record_id': 'qc', 'selection_mode': 'legacy'}), encoding='utf-8')
        self.preflight.stop()
        with patch.object(dispatch, '_production_preflight', return_value={
                'ready': False, 'reason': 'font missing', 'runtime': {}}) as check:
            result = dispatch.dispatch([{'record_id': 'qc', 'selection_mode': 'ai_editor'}],
                work=self.root, policy=self.policy, resume_stage='qc',
                runner=lambda *args: self.fail('QC must not call the model'))
        self.assertEqual(check.call_args.args[0]['selection_mode'], 'legacy')
        self.assertEqual(result[0]['stage'], 'preflight')
        self.assertEqual(result[0]['status'], 'BLOCKED')

    def test_missing_qwen_configuration_blocks_before_preparation(self):
        self.preflight.stop()
        from orchestrator.runtime import platform_env
        from orchestrator.application import cli
        env = {'JY_VISION_TRANSPORT': 'qwen', 'JY_QWEN_BASE_URL': '', 'JY_QWEN_MODEL': '',
               'JY_QWEN_API_KEY': '', 'JY_VISION_TEXT_BASE_URL': '', 'JY_VISION_TEXT_MODEL': '',
               'JY_VISION_TEXT_API_KEY': ''}
        with patch.dict(dispatch.os.environ, env), \
             patch.object(platform_env, 'environment_report', return_value={'ready': True}), \
             patch('orchestrator.runtime.headless.preflight', return_value={'status': 'READY'}), \
             patch.object(cli, '_manifest_sources', return_value=[]):
            result = dispatch.dispatch([{'record_id': 'missing'}], work=self.root, policy=self.policy,
                preparer=lambda *args: self.fail('vision must not start'),
                runner=lambda *args: self.fail('build must not start'))
        self.assertEqual(result[0]['stage'], 'preflight')
        self.assertIn('JY_QWEN_MODEL', result[0]['error'])
        self.assertIn('JY_QWEN_API_KEY', result[0]['error'])

    def test_glm_with_qwen_editor_requires_the_qwen_route_before_preparation(self):
        self.preflight.stop()
        from orchestrator.runtime import platform_env
        env = {'JY_VISION_TRANSPORT': 'glm', 'ARK_API_KEY': 'fixture-secret', 'JY_AI_EDITOR_QWEN': '1',
               'JY_VISION_TEXT_BASE_URL': '', 'JY_VISION_TEXT_MODEL': '', 'JY_VISION_TEXT_API_KEY': ''}
        with patch.dict(dispatch.os.environ, env), \
             patch.object(platform_env, 'environment_report', return_value={'ready': True}), \
             patch('orchestrator.runtime.headless.preflight', return_value={'status': 'READY'}):
            result = dispatch.dispatch([{'record_id': 'missing'}], work=self.root, policy=self.policy,
                preparer=lambda *args: self.fail('vision must not start'))
        self.assertIn('JY_VISION_TEXT_MODEL', result[0]['error'])
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_invalid_qwen_url_is_reported_without_echoing_it(self):
        from orchestrator.runtime import vision_config
        for address in ('not-a-url-secret', 'http://localhost:notaport',
                        'http://bad host', 'http://example.com:99999'):
            with self.subTest(address=address), patch.dict(dispatch.os.environ, {'JY_VISION_TRANSPORT': 'qwen',
                    'JY_QWEN_BASE_URL': address, 'JY_QWEN_MODEL': 'fixture',
                    'JY_QWEN_API_KEY': 'fixture-secret'}):
                errors = vision_config.missing_required()
            self.assertEqual(errors, ['JY_QWEN_BASE_URL_INVALID'])
            self.assertNotIn(address, str(errors))

    def test_unidentified_old_submission_is_retained_for_manual_recovery(self):
        spool = Path(self.policy.slot_dir) / 'batch_submissions'
        spool.mkdir(parents=True)
        old = spool / 'old.json'
        contents = json.dumps({'tasks': [{'record_id': 'old'}], 'work_root': str(self.root)})
        old.write_text(contents, encoding='utf-8')
        rows = dispatch.dispatch([], work=self.root, policy=self.policy,
            preparer=lambda *args: self.fail('old task must not start'))
        self.assertTrue(old.exists())
        self.assertEqual(old.read_text(encoding='utf-8'), contents)
        self.assertEqual(rows[0]['error'], 'LEGACY_SUBMISSION_REQUIRES_RECOVERY')

    def test_corrupt_old_submission_is_retained_and_reported_as_blocked(self):
        spool = Path(self.policy.slot_dir) / 'batch_submissions'
        spool.mkdir(parents=True)
        old = spool / 'damaged.json'
        old.write_text('{not-json', encoding='utf-8')
        rows = dispatch.dispatch([], work=self.root, policy=self.policy)
        self.assertEqual(rows[0]['status'], 'BLOCKED')
        self.assertEqual(Path(rows[0]['submission']), old)
        self.assertEqual(old.read_text(encoding='utf-8'), '{not-json')

    def test_old_spool_blocks_a_new_task_before_it_can_be_replayed(self):
        spool = Path(self.policy.slot_dir) / 'batch_submissions'
        spool.mkdir(parents=True)
        (spool / 'old.json').write_text(json.dumps({'tasks': [{'record_id': 'same'}]}), encoding='utf-8')
        rows = dispatch.dispatch([{'record_id': 'same'}], work=self.root, policy=self.policy,
            preparer=lambda *args: self.fail('legacy spool must block new material work'),
            runner=lambda *args: self.fail('legacy spool must block a duplicate build'))
        self.assertTrue(rows)
        self.assertTrue(all(row['status'] == 'BLOCKED' for row in rows))

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

    def test_product_group_keeps_explicit_source_order(self):
        base = {"product": "generic", "material_dir": str(self.root),
                "material_sources": [str(self.root / "weipin" / "a.mp4"),
                                     str(self.root / "paper" / "b.mp4")]}
        self.assertNotEqual(dispatch.product_key(base),
                            dispatch.product_key({**base, "material_sources": list(reversed(base["material_sources"]))}))

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

    def test_second_entry_remains_retryable_while_supervisor_is_busy(self):
        Path(self.policy.slot_dir).mkdir(parents=True, exist_ok=True)
        with file_lock(Path(self.policy.slot_dir) / "batch_dispatch", timeout=0):
            result = dispatch.dispatch([{"record_id": "queued"}], work=self.root, policy=self.policy)
        self.assertEqual(result[0]["status"], "BLOCKED")
        self.assertEqual(result[0]["error"], "DISPATCH_SUPERVISOR_BUSY_RETRY")
        self.assertTrue(result[0]["retryable"])
        self.assertFalse((Path(self.policy.slot_dir) / 'batch_submissions').exists())

    def test_dry_run_is_read_only_and_material_resume_does_not_build(self):
        work = self.root / "new"
        task = {"record_id": "a"}
        dispatch.dispatch([task], work=work, policy=self.policy, dry_run=True)
        self.assertFalse(work.exists())
        result = dispatch.dispatch([task], work=work, policy=self.policy, resume_stage="material",
            preparer=lambda *args: "shared.json",
            runner=lambda *args: self.fail("material resume invoked build"))
        self.assertEqual(result[0]["status"], "READY")

    def test_missing_or_invalid_queue_cannot_look_like_an_empty_queue(self):
        for name, contents in [('missing', None), ('invalid', '{}')]:
            path = self.root / (name + '.json')
            if contents is not None:
                path.write_text(contents, encoding='utf-8')
            proc = subprocess.run([sys.executable, '-m', 'tools.batch_dispatch', 'run',
                                   '--queue', str(path), '--work-root', str(self.root), '--dry-run'],
                                  cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(proc.returncode, 0, proc.stdout)

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

    def test_legacy_submission_is_not_replayed_automatically(self):
        submitted_at = dispatch.time.time() - 30
        with patch.object(dispatch.time, "time", return_value=submitted_at):
            path = dispatch.submit([{"record_id": "late"}], work=self.root, policy=self.policy,
                                   resume_stage="qc")
        rows = dispatch.dispatch([], work=self.root, policy=self.policy,
            preparer=lambda *args: self.fail("QC should not prepare material"),
            runner=lambda *args: self.fail("legacy task must not replay"))
        self.assertEqual(rows[0]['error'], 'LEGACY_SUBMISSION_REQUIRES_RECOVERY')
        self.assertTrue(path.is_file())

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

    def test_shared_material_failure_reports_reason_to_each_waiter(self):
        prepared, ran = [], []
        def prepare(task, directory, env):
            prepared.append(task["material_dir"])
            if task["material_dir"] == "broken":
                raise RuntimeError("MATERIAL_PREP_INCOMPLETE: NETWORK_ERROR")
            return str(directory / "ready.json")
        def run(task, stage, env, work):
            ran.append(task["record_id"])
            return {"record_id": task["record_id"], "status": "OK"}
        tasks = [{"record_id": rid, "material_dir": "broken"} for rid in ("a", "b")]
        tasks.append({"record_id": "good", "material_dir": "healthy"})
        rows = dispatch.dispatch(tasks, work=self.root, policy=self.policy,
                                 preparer=prepare, runner=run)
        blocked = [row for row in rows if row.get("status") == "BLOCKED"]
        self.assertEqual({row["record_id"] for row in blocked}, {"a", "b"})
        self.assertTrue(all(row["error"] == "MATERIAL_PREP_INCOMPLETE: NETWORK_ERROR"
                            and row["formal_deliveries"] == 0 for row in blocked))
        self.assertEqual(sorted(prepared), ["broken", "healthy"])
        self.assertEqual(ran, ["good"])
        saved = json.loads(next((self.root / "dispatch_runs").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual({r["record_id"] for r in saved["results"] if r.get("status") == "BLOCKED"}, {"a", "b"})

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

    def test_material_failure_reports_bad_rows_instead_of_inventory_tail(self):
        directory = self.root / "product"
        directory.mkdir()
        (directory / "shot_candidates.json").write_text(json.dumps({
            "issues": [{"type": "source_probe_failed", "video": "bad.MOV"}],
            "shots": [{"shot_id": "bad-shot", "status": "pending_analysis", "analysis_error": "unauditable"}],
            "material_cache": {"ready": False, "source_inventory": [{"path": "irrelevant-tail"}] * 100}}))
        import batch_worker
        with patch.object(batch_worker, "task_video_files", return_value=["fixture.MOV"]), \
             patch.object(dispatch.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout="irrelevant inventory tail", stderr="")):
            with self.assertRaisesRegex(RuntimeError, "MATERIAL_PREP_INCOMPLETE") as raised:
                dispatch.prepare_product({"record_id": "r", "script": "test"}, directory,
                                         {"JY_TASK_DEADLINE_EPOCH": str(time.time() + 60)})
        message = str(raised.exception)
        self.assertIn("source_probe_failed", message)
        self.assertIn("bad-shot", message)
        self.assertEqual(json.loads(message.split(": ", 1)[1])["analysis_file"], str(directory / "shot_candidates.json"))
        self.assertNotIn("irrelevant", message)

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

    def test_busy_supervisor_does_not_persist_another_configuration(self):
        Path(self.policy.slot_dir).mkdir(parents=True, exist_ok=True)
        with patch.dict(dispatch.os.environ, {'JY_SELECTION_MODE': 'ai_editor', 'ARK_API_KEY': 'fixture-secret'}):
            with file_lock(Path(self.policy.slot_dir) / 'batch_dispatch', timeout=0), \
                 patch.dict(dispatch.os.environ, {'JY_SELECTION_MODE': 'legacy'}):
                result = dispatch.dispatch([{'record_id': 'other'}], work=self.root, policy=self.policy)
        self.assertEqual(result[0]['status'], 'BLOCKED')
        self.assertEqual(result[0]['error'], 'DISPATCH_SUPERVISOR_BUSY_RETRY')
        self.assertNotIn('fixture-secret', json.dumps(result))

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
