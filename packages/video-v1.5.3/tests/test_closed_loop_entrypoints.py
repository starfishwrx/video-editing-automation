"""Compatibility entrypoints must retain the maintained delivery gates."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import closed_loop
from tools import closed_loop as tools_entrypoint
from tools.workflow import closed_loop as workflow


class ClosedLoopEntrypointsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="closed_loop_entrypoints_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / "task.json"
        self.done = self.root / "done.json"
        self.policy = self.root / "policy.json"
        self.report = self.root / "qc.json"
        self.draft = self.root / "draft"
        (self.draft / "media").mkdir(parents=True)
        (self.draft / "media" / "fixture.bin").write_bytes(b"synthetic")
        for name in ("draft_info.json", "fix_local.bat", "fix_local.ps1"):
            (self.draft / name).write_text("{}", encoding="utf-8")
        self.write(self.task, {"task_id": "fixture", "record_id": "rec-fixture"})
        self.write(self.done, {"status": "OK", "drafts": [str(self.draft)],
                              "nas_parent_folder": str(self.root / "shared"),
                              "qc_reports": [{"report": str(self.report)}]})
        self.write(self.policy, {"policy_id": "fixture", "policy_version": "fixture",
                                "qc": {"formal_delivery_requires": ["PASS"]},
                                "delivery": {"mac_review_gate": {
                                    "required_before_nas_and_table_write": True}},
                                "table_write": {"initial_success_status": "pending-review",
                                                "revision_status": "revision-review"}})
        self.write(self.report, {"status": "PASS"})
        self.write(self.root / "mac_review_approved.json", {"approved": True})
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_imports_share_one_implementation(self):
        self.assertIs(closed_loop, workflow)
        self.assertIs(tools_entrypoint, workflow)
        self.assertIs(closed_loop.audit, workflow.audit)
        self.assertEqual(workflow.PIPE, ROOT)
        self.assertEqual(workflow.DEFAULT_POLICY,
                         ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json")

    def test_patch_through_legacy_import_changes_real_function_globals(self):
        with mock.patch.object(closed_loop, "audit", return_value={"eligible": True}) as gate:
            result = tools_entrypoint.table_update(self.task, self.done, self.policy)
        gate.assert_called_once_with(self.task, self.done, self.policy)
        self.assertEqual(result["reason"], "LARK_BASE_TOKEN_MISSING")

    def test_unregistered_specs_keep_isolated_patchable_namespace(self):
        for index, path in enumerate((ROOT / "closed_loop.py", ROOT / "tools/closed_loop.py")):
            with self.subTest(path=path):
                name = "isolated_closed_loop_fixture_" + str(index)
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertEqual(module.PIPE, ROOT)
                self.assertIs(module.audit.__globals__, module.__dict__)
                with mock.patch.object(module, "qc_statuses", return_value=["UNCERTIFIED"]):
                    result = module.audit(self.task, self.done, self.policy)
                self.assertFalse(result["eligible"])
                self.assertIn("QC_BLOCKED", result["reasons"])
                self.assertTrue(workflow.audit(self.task, self.done, self.policy)["eligible"])

    def test_uncertified_or_failed_qc_blocks_table_without_sending(self):
        original = self.done.read_bytes()
        for status in ("FAIL", "UNCERTIFIED", "PASS_WITH_GAPS"):
            with self.subTest(status=status):
                self.write(self.report, {"status": status})
                with mock.patch.object(workflow.subprocess, "run") as send:
                    result = closed_loop.table_update(self.task, self.done, self.policy)
                self.assertEqual(result["status"], "BLOCKED")
                self.assertIn("QC_BLOCKED", result["audit"]["reasons"])
                send.assert_not_called()
        self.assertEqual(self.done.read_bytes(), original)
        self.assertFalse((self.root / "table_update_receipt.json").exists())

    def test_enforced_eval_requires_contract_even_at_root_entrypoint(self):
        with mock.patch.dict(os.environ, {"JY_EVAL_MODE": "enforce"}):
            result = closed_loop.audit(self.task, self.done, self.policy)
        self.assertFalse(result["eligible"])
        self.assertIn("EVAL_CONTRACT_MISSING", result["reasons"])

    def test_missing_manual_approval_blocks_both_imports(self):
        (self.root / "mac_review_approved.json").unlink()
        for module in (closed_loop, tools_entrypoint):
            self.assertIn("MAC_REVIEW_PENDING",
                          module.audit(self.task, self.done, self.policy)["reasons"])

    def test_script_entrypoints_work_from_unrelated_directory(self):
        for entrypoint in (ROOT / "closed_loop.py", ROOT / "tools/closed_loop.py"):
            with self.subTest(entrypoint=entrypoint):
                help_result = subprocess.run([sys.executable, "-X", "utf8", str(entrypoint), "--help"],
                                             cwd=self.root, capture_output=True, text=True,
                                             encoding="utf-8", timeout=20)
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                result = subprocess.run([sys.executable, "-X", "utf8", str(entrypoint), "audit",
                                         "--task-json", str(self.task), "--done-json", str(self.done),
                                         "--policy", str(self.policy)], cwd=self.root,
                                        capture_output=True, text=True, encoding="utf-8", timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertTrue(json.loads(result.stdout)["eligible"])

    def test_module_entrypoint_keeps_cli(self):
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "tools.closed_loop", "--help"],
                                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("failure-update", result.stdout)


if __name__ == "__main__":
    unittest.main()
