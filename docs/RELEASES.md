# Release archive notes

## 1.5.3 — draft delivery and validated-fix release workflow

- Source: `packages/video-v1.5.3/`. This PR adds the complete versioned candidate while preserving existing main-branch agent documentation and historical packages.
- Shared-path delivery verifies literal media references, bytes and project registration across current timelines, worker resume and enforce writeback audit. Generated-version recovery requires explicit confirmation and matching successful-run evidence; it creates a fresh project without changing the encrypted source.
- The validated-fix release skill ships inside the orchestrator and is registered in `SKILL_PACKAGE.json`. `tools.release_package build/verify` performs manifest/inventory checks, ZIP creation, fresh unpack and scoped or full regression checks without deployment or external publication.
- Manifest: 177 source entries. Visual policy remains independently versioned at 1.4.1.
- Fresh-unpack validation: 54 package tests and 474 orchestrator tests passed. Related delivery/transport tests are selectable for local iterations. Test counts are reported as evidence, not as a visual-quality score.
- ZIP: `releases/video-v1.5.3-draft-delivery-fix-20261016.zip`.
- ZIP SHA-256: `7fddb7803da162263fdf192e397635a9313807b73b4c3db66a2d1546166cfac6`.
- Source SHA-256: `6960d17c1595285605cdb0ce561fb4d8a1867dd5b647e3fb9ff85678b2389da8`.
- Status: source-release candidate. No production files, queues, NAS business deliveries or Feishu rows are changed by this PR. Deployment, real-task playback and human acceptance remain separate.

## 1.4.0

- Source: `packages/video-v1.4.0/`, extracted from the immutable
  `video-v1.4.0` source tag.
- The former private repository had the tag but no binary release asset. The
  downloadable ZIP was rebuilt from that tagged source; it is not represented
  as an original historical ZIP.
- Source manifest: 107 entries; all package checksum entries validated.
- Tests: 9 package tests and 158 orchestrator tests passed.
- ZIP SHA-256: `698a5668ffd234faf1f479da87afd85906c8af231f0680124d7036bfccd77df1`.

## 1.5.2

Historical archive note: the counts and ZIP hash below describe the original
1.5.2 candidate. This source tree also records subsequent quality-gate work
integrated from PR #3 and released in 1.5.3. Use the 1.5.3 candidate for the
current delivery fixes and release workflow; do not infer deployment from this history.

- Source: `packages/video-v1.5.2/`, built from the deployed 1.5.1 working tree.
- Scope: the 2026-10-12 editing review and the sanitary-product (卫品) editing instruction. Changes: no default transitions; head/tail waste trimming; category rule packs (global, 卫品, 纸品); 卫品 scene rules with phase timing and split-speed absorb tests; sentence gaps of at most 5 frames plus 1.2x 卫品 narration; per-sentence shot notes; a cross-draft shot-usage ledger. See the orchestrator CHANGELOG.
- Visual policy stays 1.4.1. The vision analysis cache version moves to `product-visual-v2`, so material pools are analysed again on first use.
- Source manifest: 159 entries.
- Tests: 40 package tests and 346 orchestrator tests passed, including scene-rule → engine → duplicate-gate → post-draft QC chain regressions for a shared absorb experiment.
- ZIP SHA-256: `bc3a458f4efe27b76023cdf0a26a533cb0fe94a2f30c85018b64c827c888f186` (`video-tool-1.5.2-8cce8009df9aded2.zip`).
- Status: candidate. Local preview builds (纸品 and 卫品, two versions each) were checked; formal delivery still needs human review.

## 1.5.1

- Source: `packages/video-v1.5.1/`.
- Visual policy stays 1.4.1. Preview black-frame fill and the production pitfalls are documented in the orchestrator skill; formal evidence gates are unchanged.
- Source manifest: 127 entries.
- Tests: 23 package tests passed. 51 orchestrator tests covering GPU encode/decode, task control, and preview black-frame fill passed.
- ZIP SHA-256: `d6f289294ca11a19854759a848801fb2364109fcf4bafa691bbe8fd5edc2dbfb`.
- Status: candidate patch. Production observation and human acceptance gates remain separate from source/package verification.

## 1.5.0

- Source: `packages/video-v1.5.0/`, extracted byte-for-byte from the candidate
  ZIP.
- Source manifest: 120 entries; all package checksum entries validated.
- Tests: 23 package tests and 197 orchestrator tests passed.
- ZIP SHA-256: `28cdb3884f52db2f1c8a8bce03a7dd97a64b7bd07fead3cf79320299be8ba961`.
- Status: candidate pre-release. Production observation and human acceptance
  gates remain separate from source/package verification.

Checksum files are stored under `releases/` and attached to the matching
GitHub Release alongside each ZIP.
