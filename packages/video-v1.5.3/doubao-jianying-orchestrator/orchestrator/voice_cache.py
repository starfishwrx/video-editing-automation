"""Extract a dated TTS panel snapshot from local Jianying resource caches.

Reads SQLite databases in read-only mode and writes only display names, voice
IDs, effect IDs, categories and tags. It does not read or export account data.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import closing
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path

PANEL = "get_panel_info_24A486D98227817A32049E4D52BEFAEF"
FIELDS = ("title", "voice_type", "effect_id", "category", "tags")


def parse_panel(raw: str) -> tuple[list[dict], dict]:
    """Validate completeness of the cached response and retain public fields."""
    payload = json.loads(raw)
    if not isinstance(payload, dict) or str(payload.get("ret")) != "0":
        raise ValueError("VOICE_PANEL_RESPONSE_INVALID")
    data = payload.get("data") or {}
    categories = data.get("categories")
    resources = data.get("category_resources")
    if not isinstance(categories, list) or not categories or not isinstance(resources, dict):
        raise ValueError("VOICE_PANEL_STRUCTURE_INVALID")
    category_names = {}
    for category in categories:
        if not isinstance(category, dict) or category.get("category_id") is None:
            raise ValueError("VOICE_PANEL_CATEGORY_INVALID")
        name = str(category.get("category_name") or "").strip()
        if not name:
            raise ValueError("VOICE_PANEL_CATEGORY_INVALID")
        category_names[str(category["category_id"])] = name
    if len(category_names) != len(categories) or set(category_names) != set(resources):
        raise ValueError("VOICE_PANEL_CATEGORY_MISSING")
    records: dict[tuple[str, str], dict] = {}
    raw_count = 0
    for category_id, group in resources.items():
        if not isinstance(group, dict) or group.get("has_more") not in (False, 0):
            raise ValueError("VOICE_PANEL_INCOMPLETE")
        items = group.get("effect_item_list")
        if not isinstance(items, list):
            raise ValueError("VOICE_PANEL_ITEMS_INVALID")
        for item in items:
            raw_count += 1
            try:
                attrs = item["common_attr"]
                extra = json.loads(attrs.get("extra") or "{}")
                tone = extra.get("tonetype") or {}
                if isinstance(tone, str):
                    tone = json.loads(tone)
                title = str(attrs.get("title") or "").strip()
                voice = str(tone.get("voice_type") or (item.get("timbre") or {}).get("voice_type") or "").strip()
                effect_id = str(attrs.get("effect_id") or attrs.get("id") or "").strip()
                if not title or not voice or not effect_id:
                    raise ValueError("missing identity")
                labels = [str(tag.get("name") or "").strip() for tag in attrs.get("tag_list") or []
                          if isinstance(tag, dict) and tag.get("name")]
            except (TypeError, KeyError, AttributeError, ValueError) as exc:
                raise ValueError("VOICE_PANEL_ITEM_INVALID") from exc
            key = (title, voice)
            record = records.setdefault(key, {"title": title, "voice_type": voice, "effect_id": effect_id,
                                               "categories": set(), "tags": set()})
            if record["effect_id"] != effect_id:
                raise ValueError("VOICE_PANEL_EFFECT_CONFLICT")
            record["categories"].add(category_names[category_id])
            record["tags"].update(labels)
    if not records:
        raise ValueError("VOICE_PANEL_EMPTY")
    preferred = ("女声", "男声", "童声", "方言", "外语")
    rows = []
    for record in records.values():
        categories_for_voice = record["categories"]
        category = next((x for x in preferred if x in categories_for_voice),
                        sorted(categories_for_voice)[0])
        tags = sorted(record["tags"] | categories_for_voice)
        rows.append({"title": record["title"], "voice_type": record["voice_type"],
                     "effect_id": record["effect_id"], "category": category, "tags": "/".join(tags)})
    rows.sort(key=lambda x: (x["title"], x["voice_type"]))
    return rows, {"categories": len(category_names), "cached_items": raw_count,
                  "unique_voices": len(rows), "response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}


def find_latest_panel(cache_root: Path) -> tuple[list[dict], dict]:
    """Use the newest complete cached panel; do not create or update a DB."""
    candidates = []
    for db in sorted(cache_root.glob("*/rp.db")):
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
                matches = connection.execute(
                    "SELECT timestamp, response_body FROM http_cache WHERE url LIKE ?", ("%" + PANEL + "%",)).fetchall()
        except (sqlite3.Error, OSError):
            continue
        for timestamp, raw in matches:
            if not isinstance(raw, str) or not timestamp:
                continue
            try:
                rows, summary = parse_panel(raw)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            candidates.append((str(timestamp), rows, summary))
    if not candidates:
        raise ValueError("VOICE_PANEL_COMPLETE_CACHE_NOT_FOUND")
    timestamp, rows, summary = max(candidates, key=lambda x: x[0])
    return rows, {**summary, "cache_timestamp": timestamp}


def write_catalog(rows: list[dict], summary: dict, output: Path) -> None:
    buffer = io.StringIO(newline="")
    buffer.write("# Jianying cached TTS panel; a local snapshot, not a live availability guarantee\n")
    buffer.write("# Cache timestamp: " + str(summary["cache_timestamp"]) + "\n")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as handle:
        handle.write(buffer.getvalue())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=Path(os.environ.get("LOCALAPPDATA", "")) /
                        "JianyingPro/User Data/Cache/ressdk_db")
    parser.add_argument("--output", type=Path, required=True, help="New CSV path; existing files are never replaced")
    args = parser.parse_args()
    rows, summary = find_latest_panel(args.cache_root)
    write_catalog(rows, summary, args.output)
    print(json.dumps({**summary, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
