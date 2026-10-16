#!/usr/bin/env python3
"""Build a deterministic manifest for the video-v1.5.3 release package."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXCLUDED_NAMES = {"build_manifest.py", "release-manifest.json", "PACKAGE_CONTENTS.sha256"}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".venv", ".uv-cache", ".git",
                  "media", "config", "backups", "validation_runs", "report", "work"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".mp4", ".mov", ".m4v", ".wav", ".mp3",
                     ".ogg", ".m4a", ".aac", ".flac", ".zip"}


def files() -> list[Path]:
    values = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if path.name in EXCLUDED_NAMES or any(part in EXCLUDED_PARTS for part in rel.parts):
            continue
        if path.name == ".env" or path.name.startswith(".env."):
            continue
        if path.is_symlink():
            raise ValueError("PACKAGE_SYMLINK_REJECTED: " + rel.as_posix())
        if path.is_file() and path.suffix.lower() not in EXCLUDED_SUFFIXES and ".bak" not in path.name:
            values.append(path)
    return sorted(values, key=lambda item: item.relative_to(ROOT).as_posix().encode("utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash(items: list[Path]) -> str:
    digest = hashlib.sha256(b"video-release-tree-v1\0")
    for path in items:
        rel = path.relative_to(ROOT).as_posix()
        mode = format(os.stat(path).st_mode & 0o7777, "04o")
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(mode.encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def main() -> int:
    items = files()
    lines = [f"{sha256(path)}  {path.relative_to(ROOT).as_posix()}" for path in items]
    (ROOT / "PACKAGE_CONTENTS.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    old = {}
    if (ROOT / "release-manifest.json").is_file():
        old = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
    manifest = {
        "schema_version": 1,
        "release_id": "video-v1.5.3",
        "version": "1.5.3",
        "tree_hash_algorithm": "video-release-tree-v1",
        "source_sha256": tree_hash(items),
        "file_count": len(items),
        "excluded": old.get("excluded") or [
            "media", "credentials", "production logs",
            "machine-specific paths", "knowledge-base bodies",
        ],
        "files": [{"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}
                  for path in items],
        "visual_policy_version": old.get("visual_policy_version") or "1.4.1",
    }
    (ROOT / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    print(manifest["source_sha256"])
    print(f"file_count={manifest['file_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
