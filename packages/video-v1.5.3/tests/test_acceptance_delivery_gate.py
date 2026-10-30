"""No preview, stale QC or failed evidence may reach distribution side effects."""
from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import production_eval as pe
from tools.acceptance import delivery_gate
from tools.workflow import closed_loop
import batch_worker as worker
from test_visual_qc_delivery_contract import qc_report
from acceptance_fixtures import make_acceptance, make_distribution


class AcceptanceDeliveryGateTests(unittest.TestCase):
    def test_worker_preserves_explicit_formal_and_default_preview(self):
        for declaration, expected in (({}, "preview"), ({"delivery_mode": "formal"}, "formal"),
                                      ({"delivery_mode": "preview"}, "preview")):
            with self.subTest(mode=expected), tempfile.TemporaryDirectory() as directory, \
                 patch.object(worker, "log"), patch.dict(worker.os.environ, {"JY_TASK_DEADLINE_TS": "", "JY_TASK_DEADLINE_EPOCH": ""}), \
                 patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as build:
                result = worker.run_build(declaration, Path(directory) / "report", "fixture", timeout=10)
                command = build.call_args.args[0]
                self.assertEqual(command[command.index("--delivery-mode") + 1], expected)
                self.assertEqual(pe.read(Path(result["manifest"]))["delivery_mode"], expected)
        with patch.object(worker.subprocess, "run") as build:
            with self.assertRaisesRegex(ValueError, "INVALID_DELIVERY_MODE"):
                worker.run_build({"delivery_mode": "unknown"}, Path("unused"), "fixture")
            build.assert_not_called()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="acceptance_gate_")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.task_dir = self.root / "work/task"
        self.task_dir.mkdir(parents=True)
        self.drafts = self.root / "drafts"
        self.draft = self.drafts / "draft-a"
        (self.draft / "media").mkdir(parents=True)
        source = self.draft / "media/source.mp4"
        source.write_bytes(b"synthetic video")
        self.write(self.draft / "draft_info.json", {"id": "draft-a-id", "tracks": [
            {"name": "Video", "type": "video", "segments": [{"material_id": "source"}]}],
            "materials": {"videos": [{"id": "source", "path": str(source), "name": source.name}]}})
        for name in ("fix_local.bat", "fix_local.ps1"):
            (self.draft / name).write_text("fixture", encoding="utf-8")
        self.report = self.task_dir / "qc.json"
        self.valid_report = qc_report(draft=self.draft)
        self.valid_report["source_bindings"] = [{"path": str(source), "identity": pe.ai_editor_contract.source_identity(source)}]
        self.write(self.report, self.valid_report)
        self.task = {"record_id": "task", "task_id": "task", "delivery_mode": "formal"}
        self.done = {"status": "OK", "drafts": [self.draft.name],
                     "qc_reports": [{"draft": self.draft.name, "report": str(self.report)}],
                     "nas_links": [str(self.draft)]}
        self.write(self.task_dir / "task.json", self.task)
        self.write(self.task_dir / "done.json", self.done)
        self.policy = self.task_dir / "policy.json"
        self.write(self.policy, {"policy_id": "fixture", "policy_version": "fixture",
            "qc": {"formal_delivery_requires": ["PASS"]},
            "delivery": {"windows_acceptance_gate": {"required_before_nas_and_table_write": True}}})

    @staticmethod
    def write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_real_current_qc_passes_and_top_level_or_wrong_draft_pass_does_not(self):
        self.assertEqual(delivery_gate.qc_statuses(self.task_dir, self.done, self.drafts), ["PASS"])
        for payload in ({"status": "PASS"}, dict(self.valid_report, draft=str(self.root / "other-draft"))):
            with self.subTest(payload=payload.get("draft")):
                self.write(self.report, payload)
                self.assertEqual(delivery_gate.qc_statuses(self.task_dir, self.done, self.drafts), ["UNCERTIFIED"])

    def test_explicit_preview_blocks_archive_and_table_without_writes(self):
        self.write(self.task_dir / "task.json", dict(self.task, delivery_mode="preview"))
        before = (self.task_dir / "done.json").read_bytes()
        with patch.object(closed_loop, "DRAFTS", self.drafts), patch.object(closed_loop.subprocess, "run") as send:
            for action in (closed_loop.archive_parent, closed_loop.table_update):
                result = action(self.task_dir / "task.json", self.task_dir / "done.json", self.policy)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertIn("PREVIEW_NOT_FORMAL", result["audit"]["reasons"])
            send.assert_not_called()
        self.assertEqual((self.task_dir / "done.json").read_bytes(), before)
        self.assertFalse((self.task_dir / "distribution_receipt.json").exists())

    def test_failed_or_uncertified_qc_blocks_before_ship_in_observe_mode(self):
        for payload in ({"status": "FAIL"}, {"status": "PASS"}, dict(self.valid_report, delivery_mode="preview")):
            self.write(self.report, payload)
            with self.subTest(status=payload["status"]), patch.object(worker, "DRAFTS", self.drafts), \
                 patch.dict(worker.os.environ, {"JY_EVAL_MODE": "observe"}), \
                 patch.object(worker, "ship_one") as ship, patch.object(worker.subprocess, "run") as external:
                result = worker.finalize_closed_loop(self.task_dir, self.task, [self.draft.name], self.done["qc_reports"])
                self.assertFalse(result["ok"])
                self.assertFalse(result["delivery_started"])
                ship.assert_not_called()
                external.assert_not_called()

    def test_observe_eval_failure_cannot_reach_ship_after_valid_qc(self):
        self.write(self.task_dir / "eval/active_contract.json", {"mode": "observe"})
        rejected = {"status": "FAIL", "criteria": [{"id": "VISUAL-QC", "status": "FAIL"}]}
        with patch.object(worker, "DRAFTS", self.drafts), \
             patch.object(worker, "CLOSED_LOOP_POLICY", self.policy), \
             patch.object(worker, "evaluate_task", return_value=rejected), patch.object(worker, "ship_one") as ship:
            result = worker.finalize_closed_loop(self.task_dir, self.task, [self.draft.name], self.done["qc_reports"])
        self.assertEqual(result["reason"], "DELIVERY_EVIDENCE_NOT_PASS")
        self.assertFalse(result["delivery_started"])
        ship.assert_not_called()

    def test_collection_loop_has_no_distribution_side_effect(self):
        import inspect
        self.assertNotIn("ship_one(", inspect.getsource(worker.process_task))

    def test_unknown_missing_or_preview_modes_never_become_formal(self):
        for declaration in ({}, {"delivery_mode": "preview"}, {"delivery_mode": "preview_only"},
                            {"delivery_mode": "预览"}, {"delivery_mode": "junk"}):
            with self.subTest(task=declaration):
                self.assertTrue(delivery_gate.mode_issues(declaration, self.done))
        self.assertEqual(delivery_gate.mode_issues(self.task, self.done), [])
        self.assertEqual(delivery_gate.mode_issues(self.task, dict(self.done, delivery_mode="invalid")),
                         ["INVALID_DELIVERY_MODE"])

    def test_explicit_preview_is_retained_uncertified_and_never_deliverable(self):
        task = {**self.task, "delivery_mode": "preview"}
        result = {"status": "UNCERTIFIED", "report": {"delivery_mode": "preview"}}
        self.assertTrue(delivery_gate.local_preview(task, result))
        self.assertFalse(delivery_gate.local_preview(self.task, result))
        self.assertFalse(delivery_gate.local_preview(task, {**result, "status": "FAIL"}))
        self.assertIn("PREVIEW_NOT_FORMAL", delivery_gate.issues(self.task_dir, task, self.done, self.drafts))

    def test_formal_without_contract_cannot_ship_even_in_observe_mode(self):
        with patch.object(worker, "DRAFTS", self.drafts), \
             patch.object(worker, "CLOSED_LOOP_POLICY", self.policy), \
             patch.object(worker, "ship_one") as ship:
            result = worker.finalize_closed_loop(self.task_dir, self.task, [self.draft.name], self.done["qc_reports"])
        self.assertEqual(result["reason"], "EVAL_CONTRACT_MISSING")
        ship.assert_not_called()


class AcceptanceModuleCompatibilityTests(unittest.TestCase):
    def test_old_imports_share_implementation_state(self):
        for old, new in (("production_eval", "evaluation"),):
            with self.subTest(module=old):
                legacy = importlib.import_module("tools." + old)
                actual = importlib.import_module("tools.acceptance." + new)
                self.assertIs(legacy, actual)
                with patch.object(legacy, "_boundary_marker", True, create=True):
                    self.assertIs(actual._boundary_marker, True)

    def test_file_import_and_all_cli_entrypoints_from_chinese_spaced_cwd(self):
        with tempfile.TemporaryDirectory(prefix="验收 模块 ") as directory:
            for name in ("production_eval",):
                path = ROOT / "tools" / (name + ".py")
                spec = importlib.util.spec_from_file_location("isolated_" + name, path)
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                self.assertIs(loaded.main.__globals__, loaded.__dict__)
                run = subprocess.run([sys.executable, "-X", "utf8", str(path), "--help"],
                    cwd=directory, capture_output=True, text=True, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr)

    def test_retired_handoff_entrypoints_are_absent(self):
        for name in ("eval_stage.py", "mac_review_handoff.py"):
            self.assertFalse((ROOT / "tools" / name).exists())
        for name in ("stages.py", "mac_review.py", "review_state.py"):
            self.assertFalse((ROOT / "tools" / "acceptance" / name).exists())
        self.assertFalse(hasattr(worker, "prepare_mac_review"))


class FinalizationContextTests(unittest.TestCase):
    def test_resume_rechecks_actual_evidence_and_resigns_without_wrapper_path(self):
        with tempfile.TemporaryDirectory(prefix="Windows 验收 ") as directory:
            root = Path(directory)
            task_dir, drafts = root / "work/record", root / "drafts"
            fixture = make_acceptance(task_dir, drafts, ["draft"], {
                "record_id": "record", "script": "Synthetic script", "delivery_mode": "formal"})
            receipts = make_distribution(fixture, drafts, ["draft"], root / "share")
            original_path = task_dir / "original-distribution.json"
            pe.write_once(original_path, receipts[0])
            bound = dict(receipts[0], receipt_path=str(original_path))
            AcceptanceDeliveryGateTests.write(task_dir / "done.json", dict(
                fixture["done"], distribution_receipts=[bound]))
            context = worker.DeliveryContext(
                draft_root=drafts, workflow_root=ROOT, skill_root=ROOT / "doubao-jianying-orchestrator",
                work_root=root / "work", runtime_root=fixture["runtime_root"],
                closed_loop_tool=ROOT / "tools/closed_loop.py",
                closed_loop_policy=ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json", python=sys.executable)
            before_environment = dict(worker.os.environ)
            with patch.object(worker, "DRAFTS", root / "incorrect-global"), \
                 patch.object(worker, "ship_one") as ship, \
                 patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', '')):
                result = worker.finalize_closed_loop(task_dir, fixture["task"], ["draft"], fixture["qc_reports"],
                                                     context=context)
                ship.assert_not_called()
                self.assertEqual(worker.DRAFTS, root / "incorrect-global")
            self.assertTrue(result["ok"], result)
            self.assertEqual(dict(worker.os.environ), before_environment)
            done = pe.read(task_dir / "done.json")
            bound = done["distribution_receipts"][0]
            body = pe.read(Path(bound["receipt_path"]))
            self.assertNotIn("receipt_path", body)
            supplied = body.pop("receipt_sha256")
            self.assertEqual(supplied, pe.digest(body))
            with patch.object(closed_loop, "DRAFTS", drafts), \
                 patch.dict(worker.os.environ, {"JY_EVAL_RUNTIME_ROOT": str(fixture["runtime_root"])}):
                audit = closed_loop.audit(task_dir / "task.json", task_dir / "done.json", context.closed_loop_policy)
            self.assertTrue(audit["eligible"], audit)

    def test_package_and_legacy_worker_skill_roots_resolve_current_orchestrator(self):
        skill = ROOT / "doubao-jianying-orchestrator"
        for legacy in (ROOT, skill, ROOT / "jianying-editor"):
            with self.subTest(legacy=legacy), patch.dict(worker.os.environ, {
                    "JY_ORCHESTRATOR_ROOT": "", "JY_SKILL_ROOT": str(legacy)}):
                self.assertEqual(worker._worker_skill_root(), skill)

    def test_external_work_root_does_not_change_code_and_policy_root(self):
        with tempfile.TemporaryDirectory(prefix="外置 工作 ") as directory:
            environment = dict(worker.os.environ, JY_PIPE_ROOT=str(ROOT), JY_WORK_ROOT=directory,
                               JY_ORCHESTRATOR_ROOT=str(ROOT / "doubao-jianying-orchestrator"))
            code = ("import json,batch_worker as w; print(json.dumps([str(w.PIPE),"
                    "str(w.CLOSED_LOOP_TOOL),str(w.CLOSED_LOOP_POLICY),str(w.WORK)]))")
            run = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=ROOT, env=environment,
                                 capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout), [str(ROOT), str(ROOT / "tools/closed_loop.py"),
                str(ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json"), directory])


if __name__ == "__main__":
    unittest.main()
