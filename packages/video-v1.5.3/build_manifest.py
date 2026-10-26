#!/usr/bin/env python3
"""Build a deterministic manifest for the version declared by this package."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXCLUDED_NAMES = {"build_manifest.py", "release-manifest.json", "PACKAGE_CONTENTS.sha256"}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".venv", ".uv-cache", ".git",
                  "media", "config", "backups", "validation_runs", "report", "work"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".mp4", ".mov", ".m4v", ".wav", ".mp3",
                     ".ogg", ".m4a", ".aac", ".flac", ".zip"}
EXCLUDED_PREFIXES = {("jianying-editor", "data")}  # 本机同步的音色、云音乐缓存。


def files() -> list[Path]:
    values = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if path.name in EXCLUDED_NAMES or any(part in EXCLUDED_PARTS for part in rel.parts):
            continue
        if rel.parts[:2] in EXCLUDED_PREFIXES:
            continue
        if path.name == ".env" or path.name.startswith(".env."):
            continue
        if path.is_symlink():
            raise ValueError("PACKAGE_SYMLINK_REJECTED: " + rel.as_posix())
        if path.is_file() and path.suffix.lower() not in EXCLUDED_SUFFIXES and ".bak" not in path.name:
            values.append(path)
    return sorted(values, key=lambda item: item.relative_to(ROOT).as_posix().encode("utf-8"))


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("PACKAGE_VERSION_MISSING")
    items = files()
    old = {}
    if (ROOT / "release-manifest.json").is_file():
        old = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
    manifest = {
        "schema_version": 2,
        "release_id": "video-v" + version,
        "version": version,
        "file_count": len(items),
        "excluded": old.get("excluded") or [
            "media", "credentials", "production logs",
            "machine-specific paths", "knowledge-base bodies",
        ],
        "files": [{"path": path.relative_to(ROOT).as_posix()}
                  for path in items],
        "visual_policy_version": old.get("visual_policy_version") or "1.4.1",
    }
    (ROOT / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    print(manifest["release_id"])
    print(f"file_count={manifest['file_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
