---
name: jianying-editor
description: Create and inspect editable Jianying drafts through the bundled JyProject wrapper, including media, audio, captions, and safe Windows setup; route end-to-end manifest builds to doubao-jianying-orchestrator.
---

# Jianying Editor

Normative keywords follow [RFC 2119 / RFC 8174 and the existing scope rules](../doubao-jianying-orchestrator/references/requirement_levels.md). Defaults remain overridable under the existing task/category contract.

Use the bundled `scripts/jy_wrapper.py` and `pyJianYingDraft` vendor package for editable draft creation. Keep media references valid and use integer microseconds for timeline values.

For missing-media delivery or encrypted historical drafts, read
[`draft_delivery_and_recovery.md`](../doubao-jianying-orchestrator/references/draft_delivery_and_recovery.md).
Use the package's `tools.draft_delivery` entrypoints; an encrypted current
timeline from a historical edited session MUST NOT silently fall back to an old plaintext sidecar. Current native drafts use their documented codec contract. Restoring
the original generated version MUST require explicit user confirmation and a
matching successful run, and creates a new project through the bundled writer.

## Quality rules

- MUST preserve source media and write to a new draft. MUST NOT close, switch, or overwrite a draft while Jianying is running.
- MUST keep audio inside its matching video/full-timeline boundary; MUST NOT leave trailing narration after the picture ends.
- MUST check the saved draft for readable media, valid timing and visible subtitles before reporting success. Source hashes or receipt signatures do not prove these results; MUST NOT report actual playback or editing acceptance without observing it.
- SHOULD reuse valid cached metadata and generated assets, avoiding unnecessary probes or TTS synthesis. Frozen-run recovery MUST also obey its input-identity and stage-budget contract.
- For end-to-end generation, MUST hand the manifest to the sibling
  `doubao-jianying-orchestrator` and run `run.py build`. This skill supplies
  the writer API; it is not a fallback writer for agent-created JSON.
- `byted-mediakit` MAY assist with understanding or audio processing, but MUST NOT
  create, install, copy, or patch JianYing draft JSON.
- MUST NOT create task-local `gen_*.py` or `install_*.py` draft scripts or
  manually edit `draft_content.json`, `draft_info.json`, or
  `draft_meta_info.json`.

## Subtitle defaults (v1.5.0)

- All subtitle entry points (`add_text_simple`, `add_narrated_subtitles`, and
  SRT import) default to size `8`, bold, and the existing configured
  color.
- Displayed subtitle text removes Chinese and English punctuation while
  retaining letters, numbers, and necessary spaces.
- Subtitle clip position defaults to `X=0`, `Y=-0.55` in JianYing's half-canvas-
  height coordinate system. A caller MAY override these values explicitly under
  the task/category contract; the orchestrator MUST validate the effective
  values after saving. Paper defaults use pixel coordinates from the paper rules:
  `Y=-528` becomes `-0.55` at 1080×1920 (`-528 / (1920 / 2)`).

For the end-to-end manifest workflow, use the sibling `doubao-jianying-orchestrator` skill. Advanced delivery/recovery notes live under `../doubao-jianying-orchestrator/references/`; this editor package has no separate `rules/` or `references/` tree.

When reviewing a generated draft, require a separate BGM audio track when BGM
was selected, a narration track for generated/file voiceover, and a subtitle
track whose materials use structured `content` JSON with a non-empty text
payload. A JSON file that merely contains `type: title`, `text`, and
`text_style` is not an accepted subtitle implementation.
