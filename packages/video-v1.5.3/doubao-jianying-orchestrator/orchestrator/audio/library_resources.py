"""Versioned audio lookup metadata, separate from host caches and credentials."""
from __future__ import annotations

import csv
import functools
import math
from pathlib import Path

from ..paths import SKILL_ROOT

CATALOGS_ROOT = SKILL_ROOT.parent / "jianying-editor" / "resources" / "catalogs"
CATALOG_FIELDS = {
    "jianying_voice_catalog.csv": ("title", "voice_type", "tags"),
    "tts_speakers.csv": ("speaker_id", "name_hint"),
    "cloud_music_library.csv": ("music_id", "title", "duration_s", "categories"),
}


class RuntimeCatalogError(RuntimeError):
    """A missing/broken runtime resource, rather than an unknown voice or track."""


def catalog_signature(name: str) -> tuple[str, int, int]:
    if name not in CATALOG_FIELDS:
        raise ValueError("UNKNOWN_RUNTIME_CATALOG:" + name)
    path = CATALOGS_ROOT / name
    try:
        stat = path.stat()
        if not path.is_file():
            raise FileNotFoundError(path)
    except FileNotFoundError as exc:
        raise RuntimeCatalogError("RUNTIME_CATALOG_MISSING:" + name) from exc
    except OSError as exc:
        raise RuntimeCatalogError("RUNTIME_CATALOG_UNREADABLE:" + name) from exc
    return str(path), stat.st_mtime_ns, stat.st_size


@functools.lru_cache(maxsize=8)
def _read_rows(signature: tuple[str, int, int], name: str) -> tuple[dict, ...]:
    try:
        with Path(signature[0]).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != CATALOG_FIELDS[name]:
                raise ValueError("invalid columns")
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("invalid row")
                item = {key: value.strip() for key, value in row.items()}
                identity, label = CATALOG_FIELDS[name][:2]
                if not item[identity] or not item[label]:
                    raise ValueError("missing identity or name")
                if name == "cloud_music_library.csv":
                    duration = float(item["duration_s"] or 0)
                    if not math.isfinite(duration) or duration < 0:
                        raise ValueError("invalid duration")
                rows.append(item)
            if not rows:
                raise ValueError("empty catalog")
    except OSError as exc:
        raise RuntimeCatalogError("RUNTIME_CATALOG_UNREADABLE:" + name) from exc
    except (ValueError, csv.Error, UnicodeError) as exc:
        raise RuntimeCatalogError("RUNTIME_CATALOG_INVALID:" + name) from exc
    return tuple(rows)


def read_catalog(name: str) -> tuple[dict, ...]:
    # Check existence/version before every cache lookup; removing or repairing
    # a file must not leave a process using an old successful lookup table.
    return _read_rows(catalog_signature(name), name)


def catalog_report(*, require_music: bool = True) -> dict:
    catalogs, errors = {}, []
    for name in CATALOG_FIELDS:
        if name == "cloud_music_library.csv" and not require_music:
            catalogs[name] = {"status": "NOT_REQUIRED", "required": False}
            continue
        try:
            rows = read_catalog(name)
            catalogs[name] = {"status": "READY", "required": True,
                              "path": str(CATALOGS_ROOT / name), "entries": len(rows)}
        except RuntimeCatalogError as exc:
            errors.append(str(exc))
            catalogs[name] = {"status": "ENV_ERROR", "required": True, "reason": str(exc)}
    return {"status": "ENV_ERROR" if errors else "READY", "ready": not errors,
            "catalogs": catalogs, "errors": errors}
