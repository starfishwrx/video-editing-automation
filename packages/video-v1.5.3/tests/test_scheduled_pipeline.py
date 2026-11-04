"""Offline scheduling tests; no model, generation, delivery or task registration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.workflow import scheduled
from tools.process_liveness import process_created_at

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


class ScheduledPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pipeline_中文 空格_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.queue = self.root / "batch_queue.json"

    def queue_tasks(self, tasks):
        self.queue.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8-sig")

    def done(self, rid, payload):
        target = self.work / rid / "done.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")

    def test_dry_run_lists_stages_without_dispatch_or_state_or_task_lock(self):
        self.queue_tasks([{"record_id": "new"}, {"record_id": "qc", "delivery_mode": "formal"}])
        self.done("qc", {"status": "PARTIAL", "drafts": ["retained"], "resume_stage": "qc"})
        result = scheduled.run_once(self.queue, self.work, dry_run=True,
                                    dispatcher=lambda *a, **k: self.fail("dry-run dispatched"))
        self.assertEqual([row["stage"] for row in result["tasks"]], ["build", "qc"])
        self.assertFalse((self.work / "pipeline_state.json").exists())
        self.assertFalse((self.work / ".pipeline.lock").exists())
        self.assertFalse((self.work / "new").exists())
        self.assertTrue(Path(result["summary_path"]).is_file())

    def test_default_dispatch_is_full_pipeline_and_second_tick_does_not_rebuild(self):
        self.queue_tasks([{"record_id": "new", "delivery_mode": "preview"}])
        def dispatch(tasks, **kwargs):
            self.assertEqual(kwargs, {"work": self.work.resolve()})
            self.assertEqual(tasks[0]["record_id"], "new")
            self.done("new", {"status": "PREVIEW_READY", "drafts": ["local"],
                              "qc_reports": [{"status": "PASS"}]})
            return [{"record_id": "new", "status": "PREVIEW_READY"}]
        with patch("tools.batch_dispatch.dispatch", side_effect=dispatch) as mocked:
            first = scheduled.run_once(self.queue, self.work)
            second = scheduled.run_once(self.queue, self.work)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(first["tasks"][0]["stage"], "build")
        self.assertEqual(second["tasks"][0]["reason"], "PREVIEW_LOCAL_ONLY")

    def test_single_record_uses_the_same_dispatcher_as_a_batch(self):
        self.queue_tasks([{"record_id": "one"}, {"record_id": "two", "delivery_mode": "preview"}])
        calls = []
        result = scheduled.run_once(self.queue, self.work, record_id="TWO",
            dispatcher=lambda tasks, **kwargs: calls.append((tasks, kwargs)) or
                [{"record_id": "two", "status": "PREVIEW_READY"}])
        self.assertEqual([task['record_id'] for task in calls[0][0]], ['two'])
        self.assertEqual(calls[0][0][0]['delivery_mode'], 'preview')
        self.assertEqual(calls[0][1]['work'], self.work.resolve())
        self.assertEqual(result['status'], 'OK')

    def test_environment_block_does_not_consume_a_build_attempt(self):
        self.queue_tasks([{"record_id": "one"}])
        def blocked(tasks, **kwargs):
            return [{"record_id": "one", "stage": "preflight", "status": "BLOCKED",
                     "retryable": True, "error": "missing font"}]
        first = scheduled.run_once(self.queue, self.work, dispatcher=blocked)
        self.assertTrue(first['tasks'][0]['preflight_only'])
        self.assertEqual(json.loads((self.work / 'pipeline_state.json').read_text())['records']['one']['build_attempts'], 0)
        calls = []
        second = scheduled.run_once(self.queue, self.work,
            dispatcher=lambda tasks, **kwargs: calls.extend(tasks) or [{"record_id": "one", "status": "PREVIEW_READY"}])
        self.assertEqual(second['status'], 'OK')
        self.assertEqual(calls[0]['record_id'], 'one')

    def test_busy_supervisor_then_normal_retry_uses_the_same_dispatch_chain(self):
        from tools import batch_dispatch
        from orchestrator.runtime_policy import RuntimePolicy
        from orchestrator.file_safety import file_lock
        self.queue_tasks([{'record_id': 'one'}])
        policy = RuntimePolicy(task_workers=1, prep_workers=1, slot_dir=str(self.root / 'slots'))
        calls = []
        def dispatcher(tasks, **kwargs):
            return batch_dispatch.dispatch(tasks, **kwargs, policy=policy,
                preparer=lambda *args: 'fixture-analysis',
                runner=lambda task, *args: calls.append(task['record_id']) or
                    {'record_id': task['record_id'], 'status': 'PREVIEW_READY'})
        with patch.object(batch_dispatch, '_production_preflight', return_value={'ready': True, 'runtime': {}}):
            with file_lock(Path(policy.slot_dir) / 'batch_dispatch', timeout=0):
                first = scheduled.run_once(self.queue, self.work, dispatcher=dispatcher)
            self.assertTrue(first['tasks'][0]['preflight_only'])
            self.assertEqual(calls, [])
            second = scheduled.run_once(self.queue, self.work, dispatcher=dispatcher)
        self.assertEqual(second['status'], 'OK')
        self.assertEqual(calls, ['one'])

    def test_worker_recovery_preflight_block_reaches_scheduler_and_can_retry(self):
        from tools import batch_dispatch
        from orchestrator.runtime_policy import RuntimePolicy
        from types import SimpleNamespace
        self.queue_tasks([{'record_id': 'one', 'delivery_mode': 'preview'}])
        policy = RuntimePolicy(task_workers=1, prep_workers=1, slot_dir=str(self.root / 'slots'))
        calls = []
        def worker_process(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                self.done('one', {'status': 'ERROR', 'retryable': True,
                    'resume_stage': 'semantic_match', 'failures': [{'preflight_only': True}]})
            else:
                self.done('one', {'status': 'PREVIEW_READY', 'retryable': False, 'failures': []})
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        def dispatcher(tasks, **kwargs):
            return batch_dispatch.dispatch(tasks, **kwargs, policy=policy,
                preparer=lambda *args: 'fixture-analysis')
        with patch.object(batch_dispatch, '_production_preflight', return_value={'ready': True, 'runtime': {}}), \
             patch.object(batch_dispatch.subprocess, 'run', side_effect=worker_process):
            first = scheduled.run_once(self.queue, self.work, dispatcher=dispatcher)
            self.assertTrue(first['tasks'][0]['preflight_only'])
            state = json.loads((self.work / 'pipeline_state.json').read_text())
            self.assertEqual(state['records']['one']['build_attempts'], 0)
            second = scheduled.run_once(self.queue, self.work, dispatcher=dispatcher)
        self.assertEqual(len(calls), 2)
        self.assertEqual(second['status'], 'OK')

    def test_qc_and_delivery_recovery_are_bounded_and_never_build(self):
        self.queue_tasks([{"record_id": "qc", "delivery_mode": "formal"}])
        self.done("qc", {"status": "PARTIAL", "drafts": ["retained"], "resume_stage": "delivery"})
        calls = []
        def dispatch(tasks, **kwargs):
            calls.append(kwargs)
            return [{"record_id": "qc", "status": "PARTIAL"}]
        for _ in range(3):
            result = scheduled.run_once(self.queue, self.work, dispatcher=dispatch)
        self.assertEqual(len(calls), scheduled.MAX_QC_ATTEMPTS)
        self.assertTrue(all(row["resume_stage"] == "qc" for row in calls))
        self.assertEqual(result["tasks"][0]["reason"], "QC_RETRY_LIMIT")
        self.assertEqual(json.loads((self.work / "qc/done.json").read_text())["status"], "PARTIAL")

    def test_verified_copy_with_pending_archive_or_table_resumes_delivery(self):
        self.queue_tasks([{"record_id": "pending", "delivery_mode": "formal"}])
        self.done("pending", {"status": "PARTIAL", "drafts": ["retained"],
            "distribution_status": "SHIPPED_VERIFIED", "qc_reports": [{"status": "PASS"}]})
        calls = []
        result = scheduled.run_once(self.queue, self.work,
            dispatcher=lambda tasks, **kwargs: calls.append(kwargs) or [{"record_id": "pending"}])
        self.assertEqual(result["tasks"][0]["stage"], "qc")
        self.assertEqual(calls[0]["resume_stage"], "qc")

    def test_failures_are_not_implicitly_retried_and_transient_build_is_bounded(self):
        self.queue_tasks([{"record_id": "hard"}, {"record_id": "transient"}])
        self.done("hard", {"status": "ERROR", "retryable": False})
        self.done("transient", {"status": "ERROR", "retryable": True})
        called = []
        def dispatch(tasks, **kwargs):
            called.extend(row["record_id"] for row in tasks)
            return [{"record_id": "transient", "status": "ERROR", "retryable": True}]
        for _ in range(3):
            result = scheduled.run_once(self.queue, self.work, dispatcher=dispatch)
        self.assertEqual(called, ["transient"] * scheduled.MAX_BUILD_ATTEMPTS)
        self.assertEqual(result["tasks"][0]["reason"], "FAILED_OR_ATTEMPTED_REQUIRES_RECOVERY")
        self.assertEqual(result["tasks"][1]["reason"], "BUILD_RETRY_LIMIT")

    def test_unsafe_duplicate_live_completed_failed_qc_and_partial_are_skipped(self):
        self.queue_tasks([{"record_id": rid} for rid in
                          ("../escape", "..", "CON", "A:", "one/two", "live", "done", "qc", "partial", "same", "SAME")])
        self.done("live", {})
        (self.work / "live/.task_run.lock").write_text(json.dumps({
            "pid": os.getpid(), "process_created_at": process_created_at(os.getpid())}))
        self.done("done", {"status": "OK"})
        self.done("qc", {"status": "PARTIAL", "drafts": ["local"], "qc_reports": [{"status": "FAIL"}]})
        self.done("partial", {"status": "PARTIAL", "drafts": ["local"]})
        tasks = json.loads(self.queue.read_text(encoding="utf-8-sig"))
        tasks[-3]["count"] = 2
        self.queue_tasks(tasks)
        result = scheduled.run_once(self.queue, self.work, dry_run=True)
        reasons = [row["reason"] for row in result["tasks"]]
        self.assertEqual(reasons[:5], ["INVALID_RECORD_ID"] * 5)
        self.assertEqual(reasons[5:9], ["TASK_ALREADY_RUNNING", "ALREADY_COMPLETED",
                                      "QC_FAILED_REQUIRES_RECOVERY", "PARTIAL_BUILD_REQUIRES_RECOVERY"])
        self.assertEqual(reasons[-1], "DUPLICATE_RECORD_ID")

    def test_malformed_receipt_is_isolated_from_other_task(self):
        self.queue_tasks([{"record_id": "bad"}, {"record_id": "good"}])
        self.done("bad", {})
        (self.work / "bad/done.json").write_text("broken")
        selected = []
        result = scheduled.run_once(self.queue, self.work,
            dispatcher=lambda tasks, **kwargs: selected.extend(tasks) or [{"record_id": "good"}])
        self.assertEqual([row["record_id"] for row in selected], ["good"])
        self.assertEqual(result["tasks"][0]["status"], "ERROR")

    def test_real_dispatch_reuses_product_analysis_and_contains_task_exception(self):
        from tools import batch_dispatch
        from orchestrator.runtime_policy import RuntimePolicy
        self.queue_tasks([{"record_id": rid, "material_dir": "same"} for rid in ("bad", "good")])
        policy = RuntimePolicy(task_workers=1, prep_workers=1, slot_dir=str(self.root / "slots"))
        prepared, ran = [], []
        def runner(task, stage, env, work):
            ran.append(task["record_id"])
            if task["record_id"] == "bad":
                raise RuntimeError("offline individual task failed")
            self.assertEqual(stage, "build")
            self.assertIn("JY_SHARED_ANALYSIS_FILE", env)
            return {"record_id": "good", "status": "PREVIEW_READY"}
        def dispatcher(tasks, **kwargs):
            return batch_dispatch.dispatch(tasks, **kwargs, policy=policy,
                preparer=lambda *args: prepared.append(True) or "offline-analysis.json", runner=runner)
        with patch.object(batch_dispatch, "_production_preflight", return_value={"ready": True, "runtime": {}}):
            result = scheduled.run_once(self.queue, self.work, dispatcher=dispatcher)
        self.assertEqual(prepared, [True])
        self.assertEqual(ran, ["bad", "good"])
        self.assertEqual(result["status"], "ERROR")
        self.assertTrue(any(row.get("record_id") == "good" for row in result["dispatch_results"]))

    def test_attempt_is_persisted_before_exception_and_not_repeated(self):
        self.queue_tasks([{"record_id": "new"}])
        def fail(tasks, **kwargs):
            state = json.loads((self.work / "pipeline_state.json").read_text())
            self.assertEqual(state["records"]["new"]["build_attempts"], 1)
            raise RuntimeError("offline failure")
        first = scheduled.run_once(self.queue, self.work, dispatcher=fail)
        second = scheduled.run_once(self.queue, self.work,
            dispatcher=lambda *a, **k: self.fail("unexpected automatic rebuild"))
        self.assertEqual(first["status"], "ERROR")
        self.assertEqual(second["tasks"][0]["reason"], "FAILED_OR_ATTEMPTED_REQUIRES_RECOVERY")

    def test_changed_task_cannot_reset_attempt_budget(self):
        self.queue_tasks([{"record_id": "new", "script": "first"}])
        scheduled.run_once(self.queue, self.work, dispatcher=lambda *a, **k: [{"record_id": "new"}])
        self.queue_tasks([{"record_id": "new", "script": "changed"}])
        result = scheduled.run_once(self.queue, self.work,
            dispatcher=lambda *a, **k: self.fail("changed task was regenerated"))
        self.assertEqual(result["tasks"][0]["reason"], "TASK_INPUT_CHANGED_REQUIRES_RESET")

    def test_record_case_change_cannot_reset_attempt_budget_across_ticks(self):
        self.queue_tasks([{"record_id": "MiXeD"}])
        scheduled.run_once(self.queue, self.work, dispatcher=lambda *a, **k: [{"record_id": "MiXeD"}])
        self.queue_tasks([{"record_id": "MIXED"}])
        result = scheduled.run_once(self.queue, self.work,
            dispatcher=lambda *a, **k: self.fail("case change bypassed frozen input"))
        self.assertEqual(result["tasks"][0]["reason"], "TASK_INPUT_CHANGED_REQUIRES_RESET")
        records = json.loads((self.work / "pipeline_state.json").read_text())["records"]
        self.assertEqual(list(records), ["mixed"])
        self.assertEqual(records["mixed"]["build_attempts"], 1)

    def test_invalid_queue_or_state_refuses_all_dispatch(self):
        self.queue_tasks({"tasks": "invalid"})
        self.assertEqual(scheduled.run_once(self.queue, self.work)["status"], "ERROR")
        self.queue_tasks([{"record_id": "new"}])
        (self.work / "pipeline_state.json").write_text("not-json")
        result = scheduled.run_once(self.queue, self.work,
                                    dispatcher=lambda *a, **k: self.fail("corrupt state dispatched"))
        self.assertEqual(result["status"], "ERROR")

    def test_other_process_lock_refuses_dispatch(self):
        self.queue_tasks([{"record_id": "new"}])
        with scheduled._pipeline_lock(self.work):
            code = ("import json; from tools.workflow.scheduled import run_once; "
                    "print(json.dumps(run_once(" + repr(str(self.queue)) + "," + repr(str(self.work)) + ")))")
            proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=ROOT,
                                  capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["reason"], "PIPELINE_ALREADY_RUNNING")
        self.assertFalse((self.work / "pipeline_state.json").exists())

    @unittest.skipUnless(POWERSHELL, "PowerShell is unavailable")
    def test_copied_example_config_points_at_the_editor_skill(self):
        package = self.root / "包 空格"
        work = package / "work"
        work.mkdir(parents=True)
        config = work / "config.local.ps1"
        shutil.copyfile(ROOT / "examples/config.example.ps1", config)
        command = (". '" + str(config).replace("'", "''") + "'; "
                   "@{skill=$env:JY_SKILL_ROOT; source=$env:JY_CONFIG_SOURCE} | ConvertTo-Json -Compress")
        proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(Path(result['skill']).resolve(), (package / 'jianying-editor').resolve())
        self.assertEqual(Path(result['source']).resolve(), config.resolve())

    @unittest.skipUnless(POWERSHELL, "PowerShell is unavailable")
    def test_powershell_runner_handles_unicode_spaces_dry_run(self):
        self.queue_tasks([{"record_id": "offline"}])
        config = self.root / "配置 空格.ps1"
        config.write_text("$env:JY_DRAFT_ROOT = 'wrong-config-draft'\n", encoding="utf-8-sig")
        env = dict(os.environ, JY_DRAFT_ROOT="wrong-parent-draft")
        proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
            str(ROOT / "scripts/run_pipeline.ps1"), "-PythonPath", sys.executable,
            "-WorkRoot", str(self.work), "-DraftRoot", str(self.root / "本地 草稿"),
            "-QueuePath", str(self.queue), "-ConfigPath", str(config), "-DryRun"],
            cwd=self.root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["tasks"][0]["stage"], "build")
        self.assertFalse((self.work / "pipeline_state.json").exists())

    @unittest.skipUnless(POWERSHELL, "PowerShell is unavailable")
    def test_powershell_runner_pins_execution_and_runtime_roots_in_real_python(self):
        package = self.root / "包 空格"
        scripts = package / "scripts"
        scripts.mkdir(parents=True)
        runner = scripts / "run_pipeline.ps1"
        shutil.copyfile(ROOT / "scripts/run_pipeline.ps1", runner)
        module = package / "tools/workflow"
        module.mkdir(parents=True)
        (module / "scheduled.py").write_text(
            "import json,os; print(json.dumps({k:os.environ.get(k) for k in "
            "['JY_PIPE_ROOT','JY_EVAL_RUNTIME_ROOT','JY_ORCHESTRATOR_ROOT','JY_SKILL_ROOT',"
            "'JY_PYTHON_EXE','JY_WORK_ROOT','JY_DRAFT_ROOT']},ensure_ascii=False))", encoding="utf-8")
        config = self.root / "config.local.ps1"
        config.write_text("$env:JY_SKILL_ROOT = '" + str(package).replace("'", "''") + "'\n"
                          "$env:JY_DRAFT_ROOT = 'wrong-config-root'\n", encoding="utf-8-sig")
        env = dict(os.environ, JY_PIPE_ROOT="old-host", JY_EVAL_RUNTIME_ROOT="old-host",
                   JY_ORCHESTRATOR_ROOT="old-host", JY_PYTHON_EXE="old-python")
        proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(runner),
            "-PythonPath", sys.executable, "-WorkRoot", str(self.work),
            "-DraftRoot", str(self.root / "剪映 草稿"), "-ConfigPath", str(config), "-DryRun"],
            cwd=self.root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(Path(result["JY_PIPE_ROOT"]).resolve(), package.resolve())
        self.assertEqual(Path(result["JY_EVAL_RUNTIME_ROOT"]).resolve(), package.resolve())
        self.assertEqual(Path(result["JY_ORCHESTRATOR_ROOT"]).resolve(), (package / "doubao-jianying-orchestrator").resolve())
        self.assertEqual(result["JY_SKILL_ROOT"], str(package))
        self.assertEqual(Path(result["JY_PYTHON_EXE"]).resolve(), Path(sys.executable).resolve())
        self.assertEqual(Path(result["JY_WORK_ROOT"]).resolve(), self.work.resolve())
        self.assertEqual(Path(result["JY_DRAFT_ROOT"]).resolve(), (self.root / "剪映 草稿").resolve())

    @unittest.skipUnless(POWERSHELL, "PowerShell is unavailable")
    def test_install_script_constructs_hidden_task_without_real_registration(self):
        # Execute a copy with scheduler commands shadowed by inert functions.
        text = (ROOT / "scripts/install_pipeline_task.ps1").read_text(encoding="utf-8")
        text = text.replace("[Security.Principal.WindowsIdentity]::GetCurrent().Name", "'offline-user'")
        script = self.root / "install_fixture.ps1"
        script.write_text(text.replace("$PSScriptRoot", "'" + str(ROOT / "scripts").replace("'", "''") + "'"), encoding="utf-8-sig")
        wrapper = self.root / "wrapper.ps1"
        wrapper.write_text(r'''
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory) return @{execute=$Execute; arguments=$Argument; cwd=$WorkingDirectory} }
function New-ScheduledTaskTrigger { param($Once,$At,$RepetitionInterval) return @{interval=$RepetitionInterval.TotalMinutes} }
function New-ScheduledTaskSettingsSet { param($MultipleInstances,[switch]$StartWhenAvailable) return @{instances=$MultipleInstances} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) return @{user=$UserId} }
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Settings,$Principal,[switch]$Force) @{action=$Action;trigger=$Trigger;settings=$Settings} | ConvertTo-Json -Depth 5 }
& $args[0] -PythonPath $args[1] -WorkRoot $args[2] -DraftRoot $args[3]
'''.replace("param($Once,$At", "param([switch]$Once,$At"), encoding="utf-8-sig")
        proc = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(wrapper),
            str(script), sys.executable, str(self.work), str(self.root / "中文 草稿")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertIn("-NoProfile -NonInteractive -WindowStyle Hidden", result["action"]["arguments"])
        self.assertIn(str((self.root / "中文 草稿").resolve()), result["action"]["arguments"])
        self.assertEqual(result["trigger"]["interval"], 5)
        self.assertEqual(result["settings"]["instances"], "IgnoreNew")
        if os.name == "nt":
            # Run exactly the generated Windows command line with dry-run
            # added. Scheduler functions above remain inert and unregistered.
            self.queue = self.work / "batch_queue.json"
            self.work.mkdir(parents=True, exist_ok=True)
            self.queue_tasks([{"record_id": "offline"}])
            command = '"' + result["action"]["execute"] + '" ' + result["action"]["arguments"] + ' -DryRun'
            action = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(action.returncode, 0, action.stdout + action.stderr)
            self.assertEqual(json.loads(action.stdout)["tasks"][0]["record_id"], "offline")


if __name__ == "__main__":
    unittest.main()
