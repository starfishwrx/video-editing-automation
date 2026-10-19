# Runtime environment contract — v1.5.4 experimental

## Isolated indexed tests

Install the optional local retrieval group with `uv sync --frozen --group indexing`.
This adds the locked PyTorch/Transformers stack; it does not embed model weights
in the release ZIP. SigLIP 2 weights/processors use the explicitly supplied model
cache. The model revision and preprocessing signature bind cached vectors.
GPU inference starts with batch size 8 and reduces it on CUDA out-of-memory.
CPU fallback is recorded; retrieval confidence never counts as product evidence.

Use `scripts/run_v154_test.py` for the experimental build. It requires `--input`,
`--report-dir`, `--index-dir`, `--model-cache-dir` and `--drafts-root`. The four
output roots must be separate non-nested directories outside this package and
the configured/default production draft roots. `--dry-run` validates and prints
the intended call without creating files or requesting models.

The launcher copies the manifest, resolves its relative media inputs against the
original input directory, and sets `pipeline_strategy=indexed_contextual` plus
explicit formal quality. Frozen audio files and measured durations are reused
without another voice decision; new scripts use the normal narrator stage before
planning. It invokes the packaged `run.py build`, never an installed skill.

Only the child process receives `JY_SKILL_ROOT` pointing at this package's editor,
`JY_DRAFTS_ROOT`/`JY_PROJECTS_ROOT` pointing at the test drafts, `JY_WORK_ROOT`
pointing at the test report, `JY_MATERIAL_INDEX_DIR` and `JY_VISION_MODEL_DIR`
pointing at the specified caches. The singular `JY_DRAFT_ROOT` is also supplied
for compatibility; the orchestrator's actual discovery uses the plural variable.
The original input, registry, global environment, installed skills and production
queue are untouched. Do not copy test paths into a production runtime file.

`material_index_dir` and `vision_model_cache_dir` are the equivalent manifest
fields. `index-materials` can populate the same local index without remote visual
calls. SQLite/NumPy data and representative frames stay in the index; weights
stay in the model cache. Remote vision uses the existing provider transport,
two slots, a 120-second request timeout and at most two attempts. Missing evidence
stays unresolved, never an automatic switch to legacy or degraded footage.

All test receipts keep `formal_delivery_allowed=false` and human review
`PENDING`. There is no automatic production installation, PR, NAS distribution
or Feishu writeback. See [the test plan](docs/V154_UPDATE_PLAN.md).

## Python dependencies

Use **uv** only (`uv sync` / `uv add` / `uv run`). Do not use `pip install`
against the system interpreter for this package or its deployment host.

- Package project: `packages/video-v1.5.4/pyproject.toml`
- Host project: the `pyproject.toml` under the configured `JY_PIPE_ROOT`
- Helper: `scripts/uv_sync.ps1` (package)

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
