"""Retired agent runners refuse work without touching task state or dispatching."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNERS = (ROOT / "jy_poll.ps1", ROOT / "run_task.template.ps1")
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable")
class RetiredRunnerTests(unittest.TestCase):
    def test_only_console_diagnostic_and_exit_are_executable(self):
        # Check syntax and all invocation nodes before any runtime experiment.
        # A resumed scheduler, credential reader or file helper must fail this gate.
        for path in RUNNERS:
            with self.subTest(runner=path.name):
                script = r"""
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(__PATH__, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'RETIRED_RUNNER_SYNTAX_INVALID' }
$commands = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true))
if ($commands.Count) { throw 'RETIRED_RUNNER_COMMAND_FORBIDDEN' }
$calls = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true))
foreach ($call in $calls) {
    if ($call.Member.Value -cne 'WriteLine' -or $call.Expression.Extent.Text -cne '[Console]::Error') {
        throw 'RETIRED_RUNNER_SIDE_EFFECT_CALL_FORBIDDEN'
    }
}
$envReads = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.VariableExpressionAst] -and $node.VariablePath.IsDriveQualified -and $node.VariablePath.DriveName -ieq 'env' }, $true))
if ($envReads.Count) { throw 'RETIRED_RUNNER_ENVIRONMENT_READ_FORBIDDEN' }
Write-Output 'RETIRED_RUNNER_SYNTAX_OK'
""".replace("__PATH__", _quote(path))
                result = subprocess.run(
                    [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("RETIRED_RUNNER_SYNTAX_OK", result.stdout)

    def _run_and_check(self, existing: bool):
        with tempfile.TemporaryDirectory(prefix="retired_runner_") as directory:
            root = Path(directory)
            runtime = root / "runtime"
            cwd = root / "unrelated_cwd"
            cwd.mkdir()
            if existing:
                (runtime / "work" / "fixture").mkdir(parents=True)
                (runtime / "state").mkdir()
                (runtime / "work" / "fixture" / "task.json").write_text(
                    json.dumps({"record_id": "fixture", "script": "unchanged"}), encoding="utf-8")
                (runtime / "state" / "RUNNING.lock").write_bytes(b"existing-process-lock")
            before = {p.relative_to(root).as_posix(): p.read_bytes()
                      for p in root.rglob("*") if p.is_file()}
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "PATH", "TEMP", "TMP", "COMSPEC", "PATHEXT",
                                      "USERPROFILE", "APPDATA", "LOCALAPPDATA"}}
            env.update({"JY_PIPE_ROOT": str(runtime), "JY_WORK_ROOT": str(runtime / "work"),
                        "JY_DRAFT_ROOT": str(runtime / "drafts"),
                        "LARK_BASE_TOKEN": "retired-fixture-token", "NAS_PASSWORD": "retired-fixture-password"})
            for path in RUNNERS:
                with self.subTest(runner=path.name, existing=existing):
                    result = subprocess.run(
                        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                        cwd=cwd, env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=20,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("LEGACY_RUNNER_RETIRED", result.stderr)
                    self.assertNotIn(env["LARK_BASE_TOKEN"], result.stdout + result.stderr)
                    self.assertNotIn(env["NAS_PASSWORD"], result.stdout + result.stderr)
                    after = {p.relative_to(root).as_posix(): p.read_bytes()
                             for p in root.rglob("*") if p.is_file()}
                    self.assertEqual(after, before)
                    self.assertFalse((runtime / "logs").exists())
                    self.assertFalse((runtime / "drafts").exists())
                    if not existing:
                        self.assertFalse(runtime.exists())

    def test_refuses_without_creating_runtime_directories(self):
        self._run_and_check(existing=False)

    def test_refuses_without_mutating_existing_task_or_lock(self):
        self._run_and_check(existing=True)


if __name__ == "__main__":
    unittest.main()
