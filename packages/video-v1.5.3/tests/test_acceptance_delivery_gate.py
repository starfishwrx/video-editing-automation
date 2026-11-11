"""No preview, stale QC or failed evidence may reach distribution side effects."""
from __future__ import annotations

import importlib
from contextlib import ExitStack
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

    def test_actionable_preview_and_missing_endpoint_return_to_worker(self):
        task = {"delivery_mode": "preview"}
        for row in ({"action_repair": "recut"}, {"action_repair": "replace"}, {"action_repair": "review"},
                    {"terminal_check": {"view": "unverified"}}, {}):
            result = {"status": "UNCERTIFIED", "report": {"delivery_mode": "preview", "model_review": [row]}}
            self.assertEqual(delivery_gate.local_preview(task, result), not bool(row))

    def test_uncertain_observation_stops_before_source_rejection(self):
        report = {"model_review": [{"index": 1, "action_repair": "review"}]}
        with patch.object(worker, "qc_current_refill_feedback") as lookup:
            self.assertIsNone(worker.action_refill_controls(Path("."), report, expected_sources=[]))
            lookup.assert_not_called()

    def test_current_qc_cannot_reject_or_recut_a_changed_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            rd = Path(directory)
            segments = [{"text": "same claim", "shot_id": "same-source", "source_start": 0.5}]
            self.write(rd / "shot_match_report.json", {"segments": segments})
            report = {"version": pe.visual_qc_contract.AI_REPORT_VERSION,
                      "model_review": [{"index": 1, "claim": "same claim", "match_level": "MISMATCH"}]}
            for digest in (None, pe.ai_editor_contract.digest([dict(segments[0], source_start=0.1)])):
                report["selection_sha256"] = digest
                self.assertEqual(worker.qc_rejected_shots_from_report(rd, report), {})
                self.assertIsNone(worker.action_refill_controls(rd, report, expected_sources=[]))
            report["selection_sha256"] = pe.ai_editor_contract.digest(segments)
            self.assertEqual(worker.qc_rejected_shots_from_report(rd, report), {1: {"same-source"}})

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

    def test_collection_waits_for_all_drafts_before_the_shared_delivery_gate(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"build fixture")
        (self.root / "work/collect/eval").mkdir(parents=True)
        task = dict(self.task, record_id="collect", title="collected", meeting_profile="legacy", count=2,
                    material_dir=str(self.root), script="测试文案")
        collected = []
        with ExitStack() as stack:
            for target, value in (("WORK", self.root / "work"), ("DRAFTS", self.drafts), ("SKIP_SHIP", False)):
                stack.enter_context(patch.object(worker, target, value))
            stack.enter_context(patch.dict(worker.os.environ, {
                "JY_TASK_DEADLINE_TS": "", "JY_TASK_DEADLINE_EPOCH": "", "JY_MATERIAL_INDEX_FILE": ""}))
            ship = stack.enter_context(patch.object(worker, "ship_one"))
            delivery = stack.enter_context(patch.object(worker, "finalize_closed_loop",
                return_value={"ok": False, "reason": "DELIVERY_EVIDENCE_NOT_PASS"}))
            def build(manifest, report, name, **kwargs):
                ship.assert_not_called()
                delivery.assert_not_called()
                collected.append(name)
                return {"rc": 0, "stdout": "", "stderr": ""}
            for target, value in (("_runtime_facts", {}), ("video_files", [str(source)]),
                                  ("calibrated_source_limit", 0), ("restore_qc_rejections", ({}, {})),
                                  ("build_progress_inputs", {"fixture": True}),
                                  ("validate_visual_match", (True, "ok")),
                                  ("run_post_draft_qc", {"status": "PASS", "output": "qc.json"}),
                                  ("validate_draft_package", {"ok": True}), ("evaluate_task", {}),
                                  ("publish_failure_notification", {})):
                stack.enter_context(patch.object(worker, target, return_value=value))
            for target in ("_save_frozen_task", "progress", "log"):
                stack.enter_context(patch.object(worker, target))
            stack.enter_context(patch.object(worker.production_eval, "runtime_identity", return_value={}))
            stack.enter_context(patch.object(worker.production_eval, "freeze", return_value={"mode": "enforce"}))
            stack.enter_context(patch.object(worker.production_eval, "write_once"))
            stack.enter_context(patch.object(worker, "run_build", side_effect=build))
            worker.process_task(task, admitted=True)
            ship.assert_not_called()
            delivery.assert_called_once()
        self.assertEqual(len(collected), 2)
        self.assertEqual(len(set(collected)), 2)
        self.assertTrue(all(name.startswith("collected-") for name in collected))
        self.assertEqual(delivery.call_args.args[2], collected)
        self.assertEqual(len(delivery.call_args.args[3]), 2)
        done = pe.read(self.root / "work/collect/done.json")
        self.assertEqual(done["drafts"], collected)
        self.assertEqual(done["nas_links"], [])
        self.assertEqual(done["failures"][0]["detail"]["reason"], "DELIVERY_EVIDENCE_NOT_PASS")

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
                 patch.dict(worker.os.environ, {"JY_TABLE_UPDATE_TIMEOUT_S": "900"}), \
                 patch.object(worker, "ship_one") as ship, \
                 patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', '')) as external:
                result = worker.finalize_closed_loop(task_dir, fixture["task"], ["draft"], fixture["qc_reports"],
                                                     context=context)
                ship.assert_not_called()
                self.assertEqual(worker.DRAFTS, root / "incorrect-global")
                updates = [call for call in external.call_args_list if "table-update" in call.args[0]]
                self.assertEqual(len(updates), 1)
                self.assertEqual(updates[0].kwargs["timeout"], 900)
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

    def test_table_ack_survives_finalization_and_failure_notification(self):
        for long_receipt in (False, True):
            with self.subTest(long_receipt=long_receipt), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task_dir, drafts = root / "work/record", root / "drafts"
                fixture = make_acceptance(task_dir, drafts, ["draft"], {
                    "record_id": "record", "script": "Synthetic script", "delivery_mode": "formal"})
                # BGM display titles are metadata, not filesystem basenames.
                # A long license-bearing title must not break ACK recovery.
                share, title = root / "share", "Track"
                if long_receipt:
                    title = "Licensed instrumental / " + "long credit and attribution; " * 80
                receipts = make_distribution(fixture, drafts, ["draft"], share)
                context = worker.DeliveryContext(
                    draft_root=drafts, workflow_root=ROOT, skill_root=ROOT / "doubao-jianying-orchestrator",
                    work_root=root / "work", runtime_root=fixture["runtime_root"],
                    closed_loop_tool=ROOT / "tools/closed_loop.py",
                    closed_loop_policy=ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json", python=sys.executable)
                writes, reads = [], []
                def external(command, **kwargs):
                    if "archive-parent" in command or "table-update" in command:
                        candidate = Path(command[command.index("--done-json") + 1])
                        action = closed_loop.archive_parent if "archive-parent" in command else closed_loop.table_update
                        result = action(task_dir / "task.json", candidate, context.closed_loop_policy)
                        return subprocess.CompletedProcess(command, 0 if result.get("ok", True) else 2,
                                                           json.dumps(result, ensure_ascii=False, indent=2), "")
                    if "+record-batch-update" in command:
                        writes.append(command)
                        return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")
                    self.assertIn("+record-get", command)
                    reads.append(command)
                    raise subprocess.TimeoutExpired(["redacted"], 20)
                with patch.object(closed_loop, "DRAFTS", drafts), \
                     patch.dict(worker.os.environ, {"JY_EVAL_RUNTIME_ROOT": str(fixture["runtime_root"]),
                         "LARK_BASE_TOKEN": "fixture-token", "LARK_TABLE_ID": "fixture-table", "LARK_ACTOR": "user"}), \
                     patch.object(closed_loop, "lark_cli_executable", return_value="fake-lark"), \
                     patch("tools.native_drafts.bgm_title", return_value=title), \
                     patch.object(closed_loop.time, "sleep"), patch.object(worker.subprocess, "run", side_effect=external):
                    result = worker.finalize_closed_loop(task_dir, fixture["task"], ["draft"],
                                                        fixture["qc_reports"], receipts, context=context)
                    self.assertEqual(result["reason"], "TABLE_UPDATE_FAILED", result)
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(len(reads), 3)
                    current = result["table_update_receipt"]
                    self.assertTrue(current["write_acknowledged"])
                    if long_receipt:
                        self.assertGreater(len(json.dumps(current, ensure_ascii=False, indent=2)), 2000)
                    self.assertLessEqual(len(result["output"]), 2000)
                    AcceptanceDeliveryGateTests.write(task_dir / "done.json", {
                        "status": "PARTIAL", "failures": [{"phase": "closed_loop",
                            "type": "delivery_or_table_gate", "message": "bounded diagnostic",
                            "detail": result}]})
                    deferred = closed_loop.failure_update(task_dir / "task.json", task_dir / "done.json",
                                                          context.closed_loop_policy)
                    self.assertEqual(deferred["reason"], "DELIVERY_READBACK_PENDING")
                    self.assertEqual(len(writes), 1)
                    self.assertTrue(pe.read(task_dir / "table_update_receipt.json")["write_acknowledged"])
                    fields = current["patch"]
                    response = {"ok": True, "data": {"record_id_list": ["record"],
                        "fields": list(fields), "data": [list(fields.values())], "has_more": False}}
                    with patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess(
                            [], 0, json.dumps(response), "")) as recovered_run:
                        recovered = closed_loop.table_update(task_dir / "task.json",
                            task_dir / "closed_loop_candidate.json", context.closed_loop_policy)
                    self.assertTrue(recovered["ok"], recovered)
                    self.assertEqual(recovered_run.call_count, 1)
                    self.assertIn("+record-get", recovered_run.call_args.args[0])

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
