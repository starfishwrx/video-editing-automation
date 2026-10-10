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
