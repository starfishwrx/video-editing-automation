# Runtime environment contract

This contract applies to the maintained `video-v1.5.3` source tree; read its
`VERSION` for the actual package version. The indexed 1.5.4/1.5.5 experiments
have separate environment contracts. For a first local run, see the repository
[quickstart](https://github.com/starfishwrx/video-editing-automation/blob/main/docs/quickstart.md).

## Python dependencies

Use **uv** only (`uv sync` / `uv add` / `uv run`). Do not use `pip install`
against the system interpreter for this package or its deployment host.

- Package project: `packages/video-v1.5.3/pyproject.toml`
- Host project: the `pyproject.toml` under the configured `JY_PIPE_ROOT`
- Helper: `scripts/uv_sync.ps1` (package)

Python must be **>=3.11,<3.13**, as declared in `pyproject.toml`.

## Vision and selection (current defaults)

The exact default model is **GLM-5.3-Flash**, API ID `glm-5.3-flash`.
GLM-5.3 and ChatGLM3 are different models. The implementation sends contact-sheet
image bytes to Volcengine Ark; full video files are not sent by this transport.
See [model and image-call details](https://github.com/starfishwrx/video-editing-automation/blob/main/docs/model-and-vision.md).

| Variable | Default / precedence | Purpose |
| --- | --- | --- |
| `JY_SELECTION_MODE` | `ai_editor`; manifest `selection_mode` wins | Select `ai_editor` or explicit `legacy` |
| `JY_VISION_TRANSPORT` | `volc_glm` | Direct Ark chat completions; `glm`/`volc` are aliases |
| `JY_VISION_MODEL` | `glm-5.3-flash` | Exact model ID, shared by image review and text planning |
| `ARK_API_KEY` | Required for direct calls | First credential source; process environment only |
| `JY_VOLC_API_KEY` | Used only if `ARK_API_KEY` is empty | Alternative Ark credential; process environment only |
| `JY_VOLC_BASE_URL` | `https://ark.cn-beijing.volces.com/api/plan/v3` | Base URL; code appends `/chat/completions` |
| `JY_VISION_BATCH_WORKERS` | `20` for direct Ark | Per-task API workers for material understanding and final QC |
| `JY_VISION_SLOTS` | `20` by default | Cross-process vision slots; AI editor also shares these slots |
| `JY_VISION_TEXT_WORKERS` | Same as batch workers; capped by batch workers | Concurrent text-review requests; both independent reads remain required |
| `JY_VISION_PREP_WORKERS` | `2` | Per-process media preparation workers; independent of API workers |
| `JY_VISION_PREP_SLOTS` | `2` | Shared cross-process media preparation slots, released before API calls |
| `JY_VISION_PREP_TIMEOUT` | `600` seconds | Media slot wait; timeout fails without starting an unbounded preparation |
| `JY_VISION_TIMEOUT` | `600` seconds for enrichment; `180` for AI editor when unset | Per-call timeout; enrichment enforces a 60-second minimum |
| `JY_AI_EDITOR_TIMEOUT_S` | `JY_VISION_TIMEOUT`, otherwise `180` | AI-editor-specific timeout |
| `JY_VISION_THINKING` | enabled | Do not set `disabled` for GLM-5.3-Flash |
| `JY_VISION_THINKING_BUDGET` | `2048` | Enabled-thinking budget for direct Ark calls |
| `JY_VISION_MAX_TOKENS` | `8192` for direct Ark | Completion token cap |
| `JY_VISION_CACHE_DIR` | Queue `work/.cache`; otherwise persistent user cache | Shared visual observations and completed text verification across tasks |
| `JY_VISION_CACHE_WAIT_TIMEOUT` | `7200` seconds | Wait for another task recognizing the same product; timeout never starts duplicate calls |
| `JY_VISION_RECHECK` | `reuse` | `reuse`, `vision` or `off`; explicit recheck policy |

Media preparation and API calls run in separate stages: finish all frames and
contact sheets, then start the API pool. Text review and final QC follow the
same preparation/call separation. Completed API results are validated and
checkpointed without waiting for the slowest batch. Text review starts after
the material-understanding stage, so the two pools do not add their limits.
`JY_VISION_SLOTS` is a shared total, not a per-task limit: if eight tasks should
each have up to 20 requests, explicitly configure a provider-approved shared
capacity (for example 160), while keeping media preparation slots at the
machine's tested capacity. A larger slot limit alone does not create requests.

`vision_progress.json` distinguishes `frame_prepare`, `sheet_prepare`, `vision`,
`text_prepare` and text calls, with preparation and completed-request counts.
`shot_candidates.json.vision_analysis` records stage wall times (API stages
include validation/checkpoint I/O). Source-code hashes are not collected at runtime.
These counters do not measure provider-side in-flight requests or prove a real
API speedup; offline tests exercise concurrency using synthetic responses.

Visual and verified-text observations are shared by product and source window,
not by task ID, script, draft name or shot numbering. The cache key checks the
source path/size/mtime, product identity (including SKU when supplied), model,
analysis rules and sampling/quality protocol. Matching completed observations
skip both image understanding and text reads. Explicitly rejected observations
are also reusable, but stay outside the matching pool. Provider failures and
pending/failed text verification are not completed cache entries.

Concurrent tasks for the same product coordinate through a file lock before
reading and filling the cache. Only the first cold task recognizes the material;
waiting tasks reload its saved results. Different products can still proceed
independently. Matching entries from old per-task `.cache/material_vision.json`
files in the same queue work root are imported without another model call.
`vision_analysis` reports `text_cache_hits`, `rejected_cache_hits` and cache wait
time. Source changes or incompatible observation protocols require fresh results.
Task-specific selection and independent final-cut QC still use each task's inputs.

Generic configuration is read from the **process environment**. There is no
generic `.env` auto-loader. Copy `examples/config.example.ps1` to the ignored
`work/config.local.ps1` and dot-source it in PowerShell; it preserves credentials
already supplied to the process. Keep real media, prompts, responses and caches
private. A local `env` report does not authenticate an Ark credential.

The AI editor requires `volc_glm`. `codex` and `anthropic` are explicit legacy
rollback transports, so also select `legacy` when using them. Codex uses
`JY_VISION_PROFILE`, optional `JY_VISION_PROVIDER` and `JY_VISION_MODEL`.
Anthropic uses `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and
`JY_VISION_MODEL` (or `ANTHROPIC_MODEL`), with an optional dedicated
`JY_VISION_ANTHROPIC_ENV` file loader. That backend-specific loader is not a
general `.env` loader. Verify the chosen backend's image capability separately.

## Local media and draft paths

- `FFMPEG_BIN` / `FFPROBE_BIN`: optional full executable paths. Otherwise the
  environment finder checks skill `bin/`, Jianying's bundled tools and PATH.
  On Windows, `run.py setup-ffmpeg` can prepare local portable executables.
- `JY_JIANYING_INSTALL_ROOT`: optional installed Jianying path used by the
  environment finder; separate from the source package root.
- `JY_SKILL_ROOT`: package root containing sibling `doubao-jianying-orchestrator`
  and `jianying-editor` directories.
- `JY_DRAFTS_ROOT` / `JY_PROJECTS_ROOT`: draft-library roots used by the engine.
  The deployment worker also uses `JY_DRAFT_ROOT`; set all three to the same
  isolated draft library when preparing a local preview or production launcher.

## Production integrations

Captions use the original TTS sentence text and measured final audio durations.
Tempo, edge trimming and loudness processing finish before caption timing is
read. A sentence spanning several video shots gets one continuous subtitle cue.
No ASR service, speech-recognition model, model download or ASR dependency group
is required. Missing or unreadable narration audio remains an explicit error.

`JY_MEETING_OPTIONAL_FIELDS_JSON` optionally maps **existing** Feishu fields to
`voice_tempo`, `modified_reference_links` and `modified_reference_attachments`.
See [the placeholder mapping](examples/meeting_optional_fields.example.json).
Fill each exact field name and field id from the table metadata; no columns are
created. Unconfigured references remain unknown. Invalid supplied mappings or
tempo must be corrected before dispatch. Keep the configured JSON private.

The release has no embedded credentials or host paths. A deployment may set:

| Variable | Purpose |
| --- | --- |
| `JY_PIPE_ROOT` | pipeline working root |
| `JY_WORK_ROOT` | task work directory |
| `JY_SKILL_ROOT` | deployed orchestrator/editor skill root |
| `JY_DRAFT_ROOT` | local Jianying drafts root |
| `JY_NAS_SHARE` | NAS share used by the distributor |
| `JY_NAS_USER` | NAS account name |
| `NAS_PASSWORD` | NAS password, process environment only |
| `JY_NAS_TEST_ROOT` | NAS test-output root |
| `JY_NAS_DISTRIBUTION_ROOT` | grouped distribution parent root |
| `JY_MAC_DRAFT_ROOT` | Mac review draft root |
| `JY_MAC_REVIEW_STAGE` | Windows-accessible review staging directory paired with the Mac mount; separate from final distribution |
| `JY_PRODUCT_IDENTITY_FILE` | Operator-owned JSON map of confirmed single-SKU source directories, with optional record exceptions; not packaged |
| `JY_MATERIAL_INDEX_FILE` | Optional reviewed material-index cache; when set, only approved same-SKU sources may enter the contract |
| `JY_EVAL_MODE` | `observe` until two real observations qualify; then `enforce` for new tasks |
| `JY_EVAL_OBSERVATIONS` | Hash-bound two-task observation receipt for enforcement |
| `LARK_BASE_TOKEN` | Feishu base token, process environment only |
| `LARK_TABLE_ID` | Feishu table id |

Missing credentials or deployment roots must stop the queue and yield a
bounded failure; do not add fallbacks containing real host values.

`JY_PRODUCT_IDENTITY_FILE` can bind one confirmed single-SKU source directory
for all tasks using it; `records` may override an exception:

```json
{"sources":{"<exact-UNC-from-table>":{"product":"<table-product>","category":"<category>","sku":"<sku>","material_dir":"<exact-UNC-from-table>","human_confirmed":true}},"records":{}}
```

The map stays on the production host. Missing or changed identity prevents
dispatch without writing a business failure to Feishu. The review staging
root and Mac root must refer to the same shared files through their respective
OS paths; successful staging is still `AWAITING_MAC_REVIEW`, not acceptance.
