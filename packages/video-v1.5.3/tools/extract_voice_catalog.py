"""Create a review-only voice snapshot from a selected local Jianying cache.

This manual tool never changes the runtime voice catalog or opens a database
for writing. Its output is a separate snapshot, not a runtime catalog.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import closing
from datetime import datetime, timezone
import io
import json
import math
import sqlite3
from pathlib import Path

PANEL = "get_panel_info_24A486D98227817A32049E4D52BEFAEF"
FIELDS = ("title", "voice_type", "effect_id", "category", "tags")
PACKAGED_CATALOGS = Path(__file__).resolve().parents[1] / "jianying-editor" / "resources" / "catalogs"


def parse_panel(raw: str) -> tuple[list[dict], dict]:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or str(payload.get("ret")) != "0":
        raise ValueError("VOICE_PANEL_RESPONSE_INVALID")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("VOICE_PANEL_STRUCTURE_INVALID")
    categories, resources = data.get("categories"), data.get("category_resources")
    if not isinstance(categories, list) or not categories or not isinstance(resources, dict):
        raise ValueError("VOICE_PANEL_STRUCTURE_INVALID")
    names = {}
    for item in categories:
        if not isinstance(item, dict) or item.get("category_id") is None:
            raise ValueError("VOICE_PANEL_CATEGORY_INVALID")
        name = str(item.get("category_name") or "").strip()
        if not name:
            raise ValueError("VOICE_PANEL_CATEGORY_INVALID")
        names[str(item["category_id"])] = name
    if len(names) != len(categories) or set(names) != set(resources):
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
                effect = str(attrs.get("effect_id") or attrs.get("id") or "").strip()
                if not title or not voice or not effect:
                    raise ValueError("missing identity")
                labels = [str(tag["name"]).strip() for tag in attrs.get("tag_list") or []
                          if isinstance(tag, dict) and tag.get("name")]
            except (TypeError, KeyError, AttributeError, ValueError) as exc:
                raise ValueError("VOICE_PANEL_ITEM_INVALID") from exc
            record = records.setdefault((title, voice), {
                "title": title, "voice_type": voice, "effect_id": effect,
                "categories": set(), "tags": set()})
            if record["effect_id"] != effect:
                raise ValueError("VOICE_PANEL_EFFECT_CONFLICT")
            record["categories"].add(names[category_id])
            record["tags"].update(labels)
    if not records:
        raise ValueError("VOICE_PANEL_EMPTY")
    rows = []
    for record in records.values():
        category = next((x for x in ("女声", "男声", "童声", "方言", "外语")
                         if x in record["categories"]), sorted(record["categories"])[0])
        rows.append({key: record[key] for key in ("title", "voice_type", "effect_id")} |
                    {"category": category, "tags": "/".join(sorted(record["tags"] | record["categories"]))})
    rows.sort(key=lambda item: (item["title"], item["voice_type"]))
    return rows, {"categories": len(names), "cached_items": raw_count, "unique_voices": len(rows)}


def _timestamp_key(value):
    if isinstance(value, str):
        text = value.strip()
        try:
            value = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return "local", parsed
            try:
                return "absolute", parsed.astimezone(timezone.utc).timestamp()
            except (OverflowError, OSError, ValueError):
                return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    # SQLite caches may store epoch milliseconds instead of seconds.
    if value >= 1_000_000_000_000:
        value /= 1000
    if value > 253_402_300_799:
        return None
    return "absolute", float(value)


def find_latest_panel(cache_root: Path) -> tuple[list[dict], dict]:
    """Read a structurally valid terminal panel with a read-only DB connection.

    A terminal page does not prove that the cache starts from the first page.
    """
    candidates = []
    for db in sorted(cache_root.glob("*/rp.db")):
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
                matches = connection.execute(
                    "SELECT timestamp, response_body FROM http_cache WHERE url LIKE ?", ("%" + PANEL + "%",)).fetchall()
        except (sqlite3.Error, OSError):
            continue
        for timestamp, raw in matches:
            key = _timestamp_key(timestamp)
            if not isinstance(raw, str) or key is None:
                continue
            try:
                rows, summary = parse_panel(raw)
            except (ValueError, TypeError):
                continue
            candidates.append((key[0], key[1], str(timestamp), rows, summary))
    if not candidates:
        raise ValueError("VOICE_PANEL_COMPLETE_CACHE_NOT_FOUND")
    if len({item[0] for item in candidates}) != 1:
        raise ValueError("VOICE_PANEL_TIMESTAMP_AMBIGUOUS")
    newest = max(item[1] for item in candidates)
    latest = [item for item in candidates if item[1] == newest]
    if any(item[3] != latest[0][3] for item in latest[1:]):
        raise ValueError("VOICE_PANEL_TIMESTAMP_CONFLICT")
    _, _, timestamp, rows, summary = latest[0]
    return rows, {**summary, "cache_timestamp": timestamp}


def write_snapshot(rows: list[dict], summary: dict, output: Path, *, cache_root: Path) -> None:
    destination = output.resolve()
    forbidden = (cache_root.resolve(), PACKAGED_CATALOGS.resolve())
    if any(destination == root or destination.is_relative_to(root) for root in forbidden):
        raise ValueError("VOICE_SNAPSHOT_OUTPUT_LOCATION_INVALID")
    for row in rows:
        for field in FIELDS:
            value = str(row[field])
            # CSV quoting does not prevent spreadsheet formula evaluation.
            if value.lstrip().startswith(("=", "+", "-", "@")) or any(c in value for c in "\t\r\n"):
                raise ValueError("VOICE_SNAPSHOT_UNSAFE_SPREADSHEET_CELL")
    buffer = io.StringIO(newline="")
    buffer.write("# Review-only snapshot; not a runtime catalog or completeness guarantee\n")
    buffer.write("# Cache timestamp: " + json.dumps(str(summary["cache_timestamp"]), ensure_ascii=True) + "\n")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(buffer.getvalue())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True, help="Existing Jianying ressdk_db directory")
    parser.add_argument("--output", type=Path, required=True, help="New review CSV outside cache and runtime catalogs")
    args = parser.parse_args()
    rows, summary = find_latest_panel(args.cache_root)
    write_snapshot(rows, summary, args.output, cache_root=args.cache_root)
    print(json.dumps({**summary, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
