# Current runtime contract

Normative keywords follow [requirement levels and scope](requirement_levels.md). Defaults and explicit overrides retain their existing precedence.

MUST read this source package's tool, orchestrator and editor version from [VERSION](../../VERSION); MUST verify the active installed version and actual files on the deployment host separately. The maintained directory name is historical, not a version selector. Visual policy is defined by [RULES.md](../../RULES.md). Remote vision defaults to Volc Ark `glm-5.3-flash` direct HTTP, with concurrency governed by the runtime policy. GPU decode SHOULD remain preferred across codecs, with CPU fallback on resource or slot limits and existing measured CPU paths retained. Formal no-settled gates remain; see CHANGELOG and [production_quality_and_lessons.md](production_quality_and_lessons.md). Preview fills remain uncertified.

The full model name is **GLM-5.3-Flash**, distinct from GLM-5.3 and ChatGLM3.
The default `ai_editor` rechecks candidate pixels, plans the full script and
uses independent final-cut QC v5. Old v4 reports MUST NOT certify current drafts.
Explicit `legacy` retains the old Top3,
scene-swap and QC v3 contracts; its reports MUST NOT certify an AI-editor draft.
All selection modes, including `rule_then_ai` and `legacy`, use GLM
(`volc_glm` / `glm` / `volc`) or Qwen (`qwen` / `qwen_compatible`) direct HTTP.
Codex CLI, Anthropic and CCSwitch runtime calls have been removed. Removed
transport names and unknown values MUST be rejected explicitly, without fallback.
High-resolution text verification and `JY_AI_EDITOR_QWEN` remain available.
The legacy description matcher is `vision_analyzer.match_sentences`.
Observation and phase-cache identities retain a fixed `profile="volc"` field
for compatible existing observations; it is not a runtime configuration option.
Local examples and process configuration are documented in the package
`examples/` and `ENVIRONMENT.md`.

Use the installed sibling skills for standalone operations. The deployed pipeline is selected by `JY_PIPE_ROOT`. MUST NOT select archived task runtimes or backup folders for new work. MUST read actual VERSION and release-manifest metadata before executing.

- `python -X utf8 run.py env` verifies the selected editor, wrapper, vendor, package metadata, FFmpeg, Python dependencies and draft directory.
- `python -X utf8 run.py build --input <manifest.json> --report-dir <new-attempt-directory>` is the manifest-to-draft entrypoint. Build success means a draft was written; it is not final quality acceptance.
- `python -X utf8 run.py recover --help` lists supported stage-specific recovery options. Recovery uses existing evidence rather than restarting all analysis.
- Before new production generation, MUST use the full pipeline's `tools/eval_bootstrap.py` to freeze confirmed product identity, task inputs, material hashes and runtime identity.
- The complete Windows pipeline runs through `scripts/run_pipeline.ps1` and `tools/workflow/scheduled.py`, reusing the dispatcher for material analysis, selection, TTS, draft writing, independent visual QC, Windows checks and eligible delivery.
- Python entrypoint: `python -m tools.workflow.scheduled --queue <batch_queue.json> --work-root <work-root> [--dry-run]`. `--dry-run` MUST only check/plan and MUST NOT execute production stages.
- PowerShell uses the existing private `work/config.local.ps1` and `JY_*` process configuration. Explicit `-PythonPath`, `-WorkRoot`, `-DraftRoot` and `-QueuePath` parameters win; `-ConfigPath` selects the local file. `scripts/install_pipeline_task.ps1` can explicitly register this same complete pipeline in Windows Task Scheduler.

The historical JyPoll/JyRun execution chain is retired: `jy_poll.ps1` and
`run_task.template.ps1` fail immediately without loading credentials or starting
a task. Single-record, batch and scheduled production all use the complete
Windows pipeline entrypoint above; `scripts/run_pipeline.ps1 -RecordId <id>`
selects one configured queue record. The manifest CLI is a lower-level build or
diagnostic interface and does not replace independent QC and delivery. Updating source does
not enable queues, change host scheduled tasks or replace already generated
production scripts. The scheduled entrypoint uses a cross-process lock and saves
`pipeline_runs/` plus `pipeline_state.json` under its configured work root.

The standard pipeline requires process-level deployment configuration. `JY_PIPE_ROOT` selects the pipeline; the worker's `JY_SKILL_ROOT` selects its orchestrator. The engine finds the adjacent jianying-editor. Set `JY_DRAFT_ROOT`, `JY_DRAFTS_ROOT` and `JY_PROJECTS_ROOT` to the same draft library when launching from the pipeline. Product-identity, material-index and credentials stay outside source-package manifests.

Formal Windows delivery MUST have explicit formal build provenance and PASS for
current inputs, runtime identity, draft package and independent current-output
visual QC. FAIL, missing or conflicting provenance, missing evidence and previews
MUST remain blocked from formal delivery. Explicitly authorized uncertified
previews MAY be delivered under their own contract without a formal success claim.
Directory metadata and file hashes MUST NOT substitute for actual visual observations.
Real Jianying opening/playback results MUST be reported only when actually observed;
source tests do not prove a production machine passed those checks. Retired human
prepare/collect, Gold approvals and old approval files MUST NOT be restored as
default prerequisites. Human review MUST be performed and recorded when the
specific task requires it; it MUST NOT become a default prerequisite for every task.

New queue runs MUST freeze original shot notes, their numbering basis and the
source-reuse policy. The queue defaults to 2 source uses per output video; a task
MAY override the default with an explicit positive integer, including values
greater than 2. Effective strict-v1 limits MUST be respected, while historical
unversioned recovery MUST retain its original behavior. Explicit force-rebuild
MUST preserve the old note binding before creating a new snapshot. Notes, mixed
refill, optional ranking observations, reviewed windows and task speed caps follow
the scoped [AI-editor contract](../../docs/AI_EDITOR.md#新队列与ai编辑的规则边界).

Candidate call failures MUST remain unknown rather than support evidence. Material
preparation MAY re-ask or exclude only the bounded unauditable cases in the
[AI-editor contract](../../docs/AI_EDITOR.md#素材准备的有界恢复); transport/parse failures,
exhausted budgets and missing required current-output evidence MUST stay blocking
in their applicable stage. Exclusion MUST NOT create a valid observation or waive
independent final-cut QC.

Old tasks and their receipts are historical evidence. A new runtime or changed inputs MUST require a new attempt and frozen contract. MUST NOT overwrite old results or resume historical queues merely because skills were upgraded. Recovery of the original run MUST preserve its frozen audio/input identities and persistent stage budget; missing or changed snapshots MUST NOT silently fall back to a full build.
