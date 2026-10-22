# Local v1.5 runtime contract

This source package's tool, orchestrator, and editor version is 1.5.6; read the active installed version from the deployment host. Visual policy stays at 1.4.1. v1.5.6 defaults remote vision to Volc Ark `glm-5.3-flash` direct HTTP (20-way concurrency) and GPU decode for all codecs; formal no-settled gates from 1.5.3 remain. See CHANGELOG and [production_quality_and_lessons.md](production_quality_and_lessons.md). Preview fills remain uncertified.

Use the installed sibling skills for standalone operations. The deployed pipeline is selected by `JY_PIPE_ROOT`. Do not select archived task runtimes or backup folders for new work. Read actual VERSION and release-manifest metadata before executing.

- `python -X utf8 run.py env` verifies the selected editor, wrapper, vendor, package metadata, FFmpeg, Python dependencies and draft directory.
- `python -X utf8 run.py build --input <manifest.json> --report-dir <new-attempt-directory>` is the manifest-to-draft entrypoint. Build success means a draft was written; it is not final quality acceptance.
- `python -X utf8 run.py recover --help` lists supported stage-specific recovery options. Recovery uses existing evidence rather than restarting all analysis.
- Use the full pipeline's `tools/eval_bootstrap.py` before new production generation to freeze confirmed product identity, task inputs, material hashes and runtime identity.
- `tools/eval_stage.py prepare` prepares hash-bound human review inputs; `collect` validates review evidence; `finalize` without `--deliver` evaluates without authorizing NAS or Feishu delivery.

The standard pipeline requires process-level deployment configuration. `JY_PIPE_ROOT` selects the pipeline; the worker's `JY_SKILL_ROOT` selects its orchestrator. The engine finds the adjacent jianying-editor. Set `JY_DRAFT_ROOT`, `JY_DRAFTS_ROOT` and `JY_PROJECTS_ROOT` to the same draft library when launching from the pipeline. Product-identity, material-index and credentials stay outside source-package manifests.

Observation mode preserves existing delivery gates. Strict final delivery requires real visual PASS, complete package evidence and human review. FAIL, missing evidence and degraded previews must never be relabeled as strict PASS. Enforce mode additionally requires two verified real observations from different SKUs using the same runtime identity and at least one verified cache reuse.

Old tasks and their receipts are historical evidence. A new runtime or changed inputs require a new attempt and frozen contract. Do not overwrite old results or resume historical queues merely because skills were upgraded.
