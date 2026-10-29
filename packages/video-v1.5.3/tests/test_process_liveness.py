import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.process_liveness import assert_process_alive, process_created_at


class ProcessLivenessTests(unittest.TestCase):
    def setUp(self):
        self.child = subprocess.Popen(
            [sys.executable, "-u", "-c", "import time; print('READY', flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_child)
        self.assertEqual(self.child.stdout.readline().strip(), "READY")

    def stop_child(self):
        if self.child.poll() is None:
            self.child.terminate()
        self.child.communicate(timeout=10)

    def test_repeated_checks_cannot_send_signals(self):
        created = process_created_at(self.child.pid)
        with patch("os.kill", side_effect=AssertionError("probe sent a signal")):
            for _ in range(100):
                assert_process_alive(self.child.pid, created)
        self.assertIsNone(self.child.poll())

    def test_exited_and_reused_identity_are_rejected(self):
        created = process_created_at(self.child.pid)
        with self.assertRaises(ProcessLookupError):
            assert_process_alive(self.child.pid, created - 100)
        self.assertIsNone(self.child.poll())
        self.child.terminate()
        self.child.wait(timeout=10)
        with self.assertRaises(ProcessLookupError):
            assert_process_alive(self.child.pid)
        with self.assertRaises(ProcessLookupError):
            assert_process_alive(0)

    def test_duplicate_record_keeps_owner_alive_and_lock_intact(self):
        import batch_worker
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            lock = directory / ".task_run.lock"
            owner = {"pid": self.child.pid, "run_id": "original",
                     "process_created_at": process_created_at(self.child.pid)}
            lock.write_text(json.dumps(owner), encoding="utf-8")
            with patch("os.kill", side_effect=AssertionError("lock check sent a signal")):
                with self.assertRaisesRegex(RuntimeError, "TASK_ALREADY_RUNNING"):
                    batch_worker._acquire_record_lock(directory, "duplicate")
            self.assertIsNone(self.child.poll())
            self.assertEqual(json.loads(lock.read_text()), owner)

    def test_pid_reuse_can_release_stale_record_without_touching_other_process(self):
        import batch_worker
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            lock = directory / ".task_run.lock"
            lock.write_text(json.dumps({"pid": self.child.pid, "run_id": "old",
                "process_created_at": process_created_at(self.child.pid) - 100}), encoding="utf-8")
            acquired = batch_worker._acquire_record_lock(directory, "new")
            self.assertEqual(json.loads(acquired.read_text())["run_id"], "new")
            self.assertIsNone(self.child.poll())
            batch_worker._release_record_lock(acquired, "new")

    def test_access_denied_never_authorizes_lock_cleanup(self):
        import psutil
        with patch("tools.process_liveness.psutil.Process", side_effect=psutil.AccessDenied(self.child.pid)):
            assert_process_alive(self.child.pid)

    def test_two_tasks_can_run_concurrently_through_supervisor(self):
        from tools.batch_dispatch import dispatch
        from orchestrator.runtime_policy import RuntimePolicy
        together = threading.Barrier(2)
        def run(task, stage, env, work):
            together.wait(timeout=5)
            return {"record_id": task["record_id"], "status": "TEST_ONLY"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            policy = RuntimePolicy(task_workers=2, prep_workers=1, slot_dir=str(root / "slots"))
            result = dispatch([{"record_id": "empty-a"}, {"record_id": "empty-b"}],
                work=root, policy=policy, preparer=lambda *args: "test-only-analysis.json", runner=run)
            self.assertEqual(len([row for row in result if row.get("status") == "TEST_ONLY"]), 2)

    def test_status_snapshot_sees_other_live_worker_and_preserves_terminal_precedence(self):
        import batch_worker
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); task = root / "record"; task.mkdir()
            (task / ".task_run.lock").write_text(json.dumps({"pid": self.child.pid,
                "process_created_at": process_created_at(self.child.pid), "run_id": "new"}), encoding="utf8")
            (task / "done.json").write_text(json.dumps({"status": "ERROR", "finished_at": "2026-01-01 00:00:00"}), encoding="utf8")
            progress = root / "progress.jsonl"
            progress.write_text(json.dumps({"record_id": "record", "event": "start",
                "run_id": "new", "ts": "2026-01-02 00:00:00"}) + "\n", encoding="utf8")
            snapshot = root / "snapshot.json"
            with patch.object(batch_worker, "WORK", root), patch.object(batch_worker, "PROGRESS", progress), \
                    patch.object(batch_worker, "STATUS_SNAPSHOT", snapshot), patch.object(batch_worker, "ACTIVE_RECORD_ID", ""), \
                    patch("os.kill", side_effect=AssertionError("status check sent a signal")):
                batch_worker._write_status_snapshot()
                self.assertEqual(json.loads(snapshot.read_text(encoding="utf8"))["tasks"]["record"]["state"], "RUNNING")
                (task / ".task_run.lock").unlink()
                batch_worker._write_status_snapshot()
                self.assertEqual(json.loads(snapshot.read_text(encoding="utf8"))["tasks"]["record"]["state"], "ERROR")
        self.assertIsNone(self.child.poll())

    def test_concurrent_worker_progress_and_status_are_atomic(self):
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw); env = dict(os.environ, PYTHONUTF8="1", JY_WORK_ROOT=str(work))
            source = ("import sys,batch_worker; r=sys.argv[1]; "
                      "[batch_worker.progress({'record_id':r,'event':'start','run_id':str(i)}) for i in range(8)]")
            def run(index):
                return subprocess.run([sys.executable, "-X", "utf8", "-c", source, str(index)],
                    cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf8", timeout=60)
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(run, range(3)))
            self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
            lines = (work / "batch_progress.jsonl").read_text(encoding="utf8").splitlines()
            self.assertEqual(len(lines), 24)
            self.assertEqual(len([json.loads(line) for line in lines]), 24)
            json.loads((work / "task_status_snapshot.json").read_text(encoding="utf8"))
            log = work / "batch_worker.log"
            self.assertNotIn("STATUS_SNAPSHOT_WARN", log.read_text(encoding="utf8") if log.exists() else "")

    def test_active_host_scripts_do_not_use_signal_zero_probes(self):
        offenders = []
        for folder in (ROOT, ROOT / "work"):
            for path in folder.glob("*.py"):
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
                            and node.func.attr == "kill" and len(node.args) == 2
                            and isinstance(node.args[1], ast.Constant) and node.args[1].value == 0):
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
