"""Offline regression coverage for optional JyPoll field mapping.

Only pure field-normalization and batch-identity functions are evaluated by PowerShell;
the poller's API, queue, and process-dispatch code is never executed.
"""
from __future__ import annotations

import codecs
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PACKAGE = Path(__file__).resolve().parents[1]


def _ps_quote(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class MeetingPollOptionalFieldTests(unittest.TestCase):
    def test_powershell_syntax_and_optional_field_mapping_fixture(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell is unavailable")

        jy_poll = PACKAGE / "jy_poll.ps1"
        run_task = PACKAGE / "run_task.template.ps1"
        script = r'''$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
foreach ($path in @(__JY_POLL__, __RUN_TASK__)) {
  $tokens = $null; $parseErrors = $null
  [void][System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
  if ($parseErrors.Count) { throw ("Parser errors in {0}: {1}" -f $path, ($parseErrors | ForEach-Object Message -join '; ')) }
}
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(__JY_POLL__, [ref]$tokens, [ref]$parseErrors)
$allowed = @('Convert-MeetingTempo','Resolve-MeetingOptionalFieldMap','Get-MeetingTaskInputs','Get-PollBatchState')
$definitions = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $allowed -contains $node.Name }, $true))
foreach ($name in $allowed) {
  $definition = $definitions | Where-Object Name -eq $name | Select-Object -First 1
  if (-not $definition) { throw "Missing allowlisted function $name" }
  Invoke-Expression $definition.Extent.Text
}
$baseIds = @('fldBase01','fldBase02','fldBase03','fldBase04','fldBase05','fldBase06','fldBase07','fldBase08','fldBase09','fldBase10','fldBase11','fldBase12')
$slots = @('voice_tempo','modified_reference_links','modified_reference_attachments')
$config = @'
{"voice_tempo":{"field_name":"TTS语速","field_id":"fldTempo01"},"modified_reference_links":{"field_name":"修改后成品链接","field_id":"fldLink01"},"modified_reference_attachments":{"field_name":"修改后成品附件","field_id":"fldAttach01"}}
'@
$map = Resolve-MeetingOptionalFieldMap $config $baseIds $slots
if ($map.issues.Count -ne 0) { throw "Valid field config flagged: $($map.issues -join ',')" }
$row = [System.Collections.Generic.List[object]]::new()
for ($i = 0; $i -lt 12; $i++) { $row.Add('') }
$row[4] = '原始配音填写'
$row.Add('1.0')
$row.Add('https://example.invalid/final.mp4')
$row.Add(@([pscustomobject]@{name='final.mp4'; token='fake-token'}))
$parsed = Get-MeetingTaskInputs $row $map.indices $map.fields
if ($parsed.voice_original -cne '原始配音填写') { throw 'Raw voice text was not retained' }
if ($parsed.voice_tempo_status -cne 'explicit' -or [double]$parsed.voice_tempo -ne 1.0 -or $parsed.voice_tempo_original -cne '1.0') { throw 'Explicit 1.0 tempo was not preserved' }
if ([string]$parsed.modified_reference_links -cne 'https://example.invalid/final.mp4') { throw 'Reference link mapping failed' }
if ($parsed.modified_reference_attachments.name -cne 'final.mp4') { throw 'Reference attachment mapping failed' }
if ($parsed.reference_status -cne 'unknown') { throw 'Reference presence must remain unknown' }

$missingMap = Resolve-MeetingOptionalFieldMap '' $baseIds $slots
$missingRow = [System.Collections.Generic.List[object]]::new()
for ($i = 0; $i -lt 12; $i++) { $missingRow.Add('') }
$missing = Get-MeetingTaskInputs $missingRow $missingMap.indices $missingMap.fields
if ($missing.voice_tempo_status -cne 'unknown' -or $missing.reference_status -cne 'unknown' -or @($missing.modified_reference_links).Count -ne 0 -or @($missing.modified_reference_attachments).Count -ne 0) { throw 'Absent optional fields must remain unknown and empty' }

$badConfig = '{"voice_tempo":{"field_name":"速度","field_id":"bad-id"}}'
$badMap = Resolve-MeetingOptionalFieldMap $badConfig $baseIds $slots
if ($badMap.issues.Count -eq 0) { throw 'Invalid field configuration was not flagged' }

$row[12] = '快一点'
$row[13] = ''
$row[14] = ''
$invalid = Get-MeetingTaskInputs $row $map.indices $map.fields
if ($invalid.voice_tempo_status -cne 'invalid' -or $null -ne $invalid.voice_tempo -or $invalid.voice_tempo_original -cne '快一点') { throw 'Invalid tempo must be retained and marked invalid' }
$firstBatch = Get-PollBatchState @('task-a','task-b') @{}
$remainingBatch = Get-PollBatchState @('task-b') $firstBatch
if ($remainingBatch.batch_id -cne $firstBatch.batch_id) { throw 'Completed task reset BGM batch identity' }
$joinedBatch = Get-PollBatchState @('task-b','task-c') $remainingBatch
if ($joinedBatch.batch_id -cne $firstBatch.batch_id) { throw 'Outstanding cohort did not keep its identity' }
$nextBatch = Get-PollBatchState @('task-d') $joinedBatch
if ($nextBatch.batch_id -ceq $firstBatch.batch_id) { throw 'Disjoint cohort did not get a new batch identity' }
$emptyBatch = Get-PollBatchState @() $nextBatch
$afterEmpty = Get-PollBatchState @('task-d') $emptyBatch
if ($afterEmpty.batch_id -ceq $emptyBatch.batch_id) { throw 'Drained cohort was reused' }
Write-Output 'MEETING_POLL_FIELDS_FIXTURE_OK'
'''.replace("__JY_POLL__", _ps_quote(jy_poll)).replace("__RUN_TASK__", _ps_quote(run_task))

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "meeting_poll_fixture.ps1"
            fixture.write_bytes(codecs.BOM_UTF8 + script.encode("utf-8"))
            result = subprocess.run(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(fixture)],
                check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, msg=result.stdout + "\n" + result.stderr)
        self.assertIn("MEETING_POLL_FIELDS_FIXTURE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
