# Release archive notes

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

- Source: `packages/video-v1.5.2/`, built from the deployed 1.5.1 working tree.
- Scope: the 2026-10-12 editing review and the sanitary-product (卫品) editing instruction. Changes: no default transitions; head/tail waste trimming; category rule packs (global, 卫品, 纸品); 卫品 scene rules with phase timing and split-speed absorb tests; sentence gaps of at most 5 frames plus 1.2x 卫品 narration; per-sentence shot notes; a cross-draft shot-usage ledger. See the orchestrator CHANGELOG.
- Visual policy stays 1.4.1. The vision analysis cache version moves to `product-visual-v2`, so material pools are analysed again on first use.
- Source manifest: 159 entries.
- Tests: 40 package tests and 338 orchestrator tests passed.
- ZIP SHA-256: `be6973fc2aa09572891e58225366856b65f879e2f4caf3a5940cac8fec645510` (`video-tool-1.5.2-855fc7c4b2f8fa5c.zip`).
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
