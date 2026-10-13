"""跨草稿镜头使用台账（v1.5.2）。

同一素材池的每条成稿落一次「用了哪些镜头」。选镜时：同一任务的其它版本用过的镜头
重罚（排在没用过的同级候选之后），其它任务用过的轻罚（只在分数上扣一点）。
台账只影响同级候选之间的先后，不会让一句话因此变成黑屏。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .file_safety import locked_json_update

LEDGER_DIR_ENV = "JY_USAGE_LEDGER_DIR"
OTHER_TASK_PENALTY = 8
MAX_USES_PER_SHOT = 50
SAME_TASK = "same_task"
OTHER_TASK = "other_task"


def pool_hash(manifest: dict[str, Any]) -> str:
    sources = manifest.get("material_sources") or []
    names = sorted({Path(str(s)).name.casefold() for s in sources if str(s).strip()})
    if not names:
        return ""
    return hashlib.sha1("\n".join(names).encode("utf-8")).hexdigest()[:16]


def ledger_path(manifest: dict[str, Any], report_dir: Optional[Path] = None) -> Optional[Path]:
    explicit = str(manifest.get("usage_ledger_path") or "").strip()
    if explicit:
        return Path(explicit)
    digest = pool_hash(manifest)
    if not digest:
        return None
    base = os.environ.get(LEDGER_DIR_ENV, "").strip()
    if base:
        root = Path(base)
    elif report_dir is not None:
        root = Path(report_dir).resolve().parent.parent / ".cache"
    else:
        return None
    return root / f"shot_usage_{digest}.json"


def shot_key(video: Any, source_start: Any) -> str:
    try:
        start = float(source_start or 0.0)
    except (TypeError, ValueError):
        start = 0.0
    return f"{Path(str(video or '')).name.casefold()}@{start:.2f}"


def _identity(manifest: dict[str, Any]) -> tuple[str, str]:
    task = str(manifest.get("ledger_task") or manifest.get("record_id")
               or manifest.get("task_id") or "").strip()
    draft = str(manifest.get("draft_name") or "").strip()
    return task, draft


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_usage(manifest: dict[str, Any]) -> dict[str, str]:
    """``{shot_key: "same_task" | "other_task"}``；本草稿自己的旧记录不算。"""
    raw = str(manifest.get("usage_ledger_path") or "").strip()
    if not raw:
        return {}
    data = _read(Path(raw))
    task, draft = _identity(manifest)
    out: dict[str, str] = {}
    for key, uses in (data.get("shots") or {}).items():
        for use in uses or []:
            if not isinstance(use, dict) or (draft and use.get("draft") == draft):
                continue
            if task and use.get("task") == task:
                out[key] = SAME_TASK
                break
            out.setdefault(key, OTHER_TASK)
    return out


def segment_keys(segments: Iterable[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for seg in segments or []:
        if not isinstance(seg, dict) or seg.get("visual_missing"):
            continue
        parts = seg.get("span_parts") if seg.get("multi_shot") else None
        if isinstance(parts, list) and parts:
            for part in parts:
                if isinstance(part, dict):
                    keys.append(shot_key(part.get("video") or seg.get("video"),
                                         part.get("scene_source_start", part.get("source_start"))))
        elif seg.get("video"):
            keys.append(shot_key(seg.get("video"),
                                 seg.get("scene_source_start", seg.get("source_start"))))
    return list(dict.fromkeys(keys))


def record(manifest: dict[str, Any], segments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    raw = str(manifest.get("usage_ledger_path") or "").strip()
    task, draft = _identity(manifest)
    if not raw or not draft:
        return {"recorded": 0, "path": raw}
    keys = segment_keys(segments)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    def mutate(data):
        data = data if isinstance(data, dict) else {}
        shots = data.setdefault("shots", {})
        for key in list(shots):
            shots[key] = [u for u in shots[key] or [] if u.get("draft") != draft]
            if not shots[key]:
                shots.pop(key)
        for key in keys:
            uses = shots.setdefault(key, [])
            uses.append({"task": task, "draft": draft, "at": now})
            del uses[:-MAX_USES_PER_SHOT]
        drafts = data.setdefault("drafts", {})
        drafts[draft] = {"task": task, "at": now, "shots": keys}
        data["schema"] = "shot-usage-v1"
        return data

    data = locked_json_update(Path(raw), mutate, default={})
    return {"recorded": len(keys), "path": raw,
            "overlap": overlap_for(data, draft)}


def overlap_for(data: dict[str, Any], draft: str) -> list[dict[str, Any]]:
    """本草稿与台账里其它草稿的镜头重合率（重合数 ÷ 两者中较少的镜头数）。"""
    drafts = (data or {}).get("drafts") or {}
    mine = drafts.get(draft) or {}
    mine_keys = set(mine.get("shots") or [])
    rows = []
    for other, info in drafts.items():
        if other == draft or not isinstance(info, dict):
            continue
        keys = set(info.get("shots") or [])
        if not keys or not mine_keys:
            continue
        shared = len(mine_keys & keys)
        rows.append({"draft": other, "same_task": info.get("task") == mine.get("task"),
                     "shared": shared,
                     "ratio": round(shared / float(min(len(keys), len(mine_keys))), 3)})
    rows.sort(key=lambda r: (not r["same_task"], -r["ratio"]))
    return rows[:20]
