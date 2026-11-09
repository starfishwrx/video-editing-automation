# Current runtime contract

Read this source package's tool, orchestrator and editor version from [VERSION](../../VERSION); verify the active installed version and actual files on the deployment host separately. The maintained directory name is historical, not a version selector. Visual policy is defined by [RULES.md](../../RULES.md). Remote vision defaults to Volc Ark `glm-5.3-flash` direct HTTP, with concurrency governed by the runtime policy. GPU decode remains preferred across codecs, with CPU fallback on resource or slot limits. Formal no-settled gates remain; see CHANGELOG and [production_quality_and_lessons.md](production_quality_and_lessons.md). Preview fills remain uncertified.

The full model name is **GLM-5.3-Flash**, distinct from GLM-5.3 and ChatGLM3.
The default `ai_editor` rechecks candidate pixels, plans the full script and
uses independent final-cut QC v5. Old v4 reports cannot certify current drafts.
Explicit `legacy` retains the old Top3,
scene-swap and QC v3 contracts; its reports cannot certify an AI-editor draft.
All selection modes, including `rule_then_ai` and `legacy`, use GLM
(`volc_glm` / `glm` / `volc`) or Qwen (`qwen` / `qwen_compatible`) direct HTTP.
Codex CLI, Anthropic and CCSwitch runtime calls have been removed. Removed
transport names and unknown values are rejected explicitly, without fallback.
High-resolution text verification and `JY_AI_EDITOR_QWEN` remain available.
The legacy description matcher is `vision_analyzer.match_sentences`.
Observation and phase-cache identities retain a fixed `profile="volc"` field
for compatible existing observations; it is not a runtime configuration option.
Local examples and process configuration are documented in the package
`examples/` and `ENVIRONMENT.md`.

Use the installed sibling skills for standalone operations. The deployed pipeline is selected by `JY_PIPE_ROOT`. Do not select archived task runtimes or backup folders for new work. Read actual VERSION and release-manifest metadata before executing.

- `python -X utf8 run.py env` verifies the selected editor, wrapper, vendor, package metadata, FFmpeg, Python dependencies and draft directory.
- `python -X utf8 run.py build --input <manifest.json> --report-dir <new-attempt-directory>` is the manifest-to-draft entrypoint. Build success means a draft was written; it is not final quality acceptance.
- `python -X utf8 run.py recover --help` lists supported stage-specific recovery options. Recovery uses existing evidence rather than restarting all analysis.
- Use the full pipeline's `tools/eval_bootstrap.py` before new production generation to freeze confirmed product identity, task inputs, material hashes and runtime identity.
- The complete Windows pipeline runs through `scripts/run_pipeline.ps1` and `tools/workflow/scheduled.py`, reusing the dispatcher for material analysis, selection, TTS, draft writing, independent visual QC, Windows checks and eligible delivery.
- Python entrypoint: `python -m tools.workflow.scheduled --queue <batch_queue.json> --work-root <work-root> [--dry-run]`. `--dry-run` checks/plans without executing production stages.
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

Strict Windows delivery requires explicit formal build provenance, current-output
independent visual PASS, complete package evidence and matching task/input/runtime
identity. FAIL, missing or conflicting build provenance, missing evidence and
previews remain blocked. Directory metadata and file hashes cannot substitute
for actual visual observations. Real Jianying opening/playback results are
reported only when they have actually been observed; source tests do not prove
a production machine has passed those checks.

Old tasks and their receipts are historical evidence. A new runtime or changed inputs require a new attempt and frozen contract. Do not overwrite old results or resume historical queues merely because skills were upgraded.
