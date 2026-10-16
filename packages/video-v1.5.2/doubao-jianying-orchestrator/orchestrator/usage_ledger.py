"""跨草稿镜头使用台账（v1.5.2）。

同一素材池的每条成稿落一次「用了哪些镜头」。选镜时：同一任务的其它版本用过的镜头
重罚（排在没用过的同级候选之后）；其它任务用过的，展示句（开场、品牌、促销）同样往后排，
用过的任务越多越靠后，效果演示句只轻罚（同一个演示可以复用）。
选完镜就先占位，并行跑的任务立刻能看到；成稿后改成正式记录，失败则撤掉占位。
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
# 占位比最长构建时间再留些余量；过期的占位是崩掉没撤的任务留下的。
RESERVATION_TTL_S = 4 * 3600
SAME_TASK = "same_task"
OTHER_TASK = "other_task"


def _norm_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").casefold()


def pool_hash(manifest: dict[str, Any]) -> str:
    # 手机素材常同名（IMG_0001.MOV），只看文件名会让不同 SKU 的素材池串账。
    sources = manifest.get("material_sources") or []
    paths = sorted({_norm_path(s) for s in sources if str(s).strip()})
    if not paths:
        return ""
    return hashlib.sha1("\n".join(paths).encode("utf-8")).hexdigest()[:16]


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
        # 固定落在 work/.cache：重试多套一层目录也是同一本账。
        resolved = Path(report_dir).resolve()
        work = next((p for p in resolved.parents if p.name.casefold() == "work"), None)
        root = (work or resolved.parent.parent) / ".cache"
    else:
        return None
    return root / f"shot_usage_{digest}.json"


def shot_key(video: Any, source_start: Any) -> str:
    try:
        start = float(source_start or 0.0)
    except (TypeError, ValueError):
        start = 0.0
    path = _norm_path(video)
    folder, _, name = path.rpartition("/")
    tag = hashlib.sha1(folder.encode("utf-8")).hexdigest()[:8] if folder else "-"
    return f"{tag}/{name}@{start:.2f}"


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


def _live(use: Any, now: float) -> bool:
    if not isinstance(use, dict):
        return False
    if not use.get("reserved"):
        return True
    try:
        at = time.mktime(time.strptime(str(use.get("at") or ""), "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return False
    return now - at <= RESERVATION_TTL_S


def _other_uses(manifest: dict[str, Any]):
    raw = str(manifest.get("usage_ledger_path") or "").strip()
    if not raw:
        return
    data = _read(Path(raw))
    task, draft = _identity(manifest)
    now = time.time()
    for key, uses in (data.get("shots") or {}).items():
        for use in uses or []:
            if _live(use, now) and not (draft and use.get("draft") == draft):
                yield key, use, bool(task and use.get("task") == task)


def load_usage(manifest: dict[str, Any]) -> dict[str, str]:
    """``{shot_key: "same_task" | "other_task"}``；本草稿自己的旧记录不算。"""
    out: dict[str, str] = {}
    for key, _use, same in _other_uses(manifest):
        if same:
            out[key] = SAME_TASK
        else:
            out.setdefault(key, OTHER_TASK)
    return out


def other_task_counts(manifest: dict[str, Any]) -> dict[str, int]:
    """``{shot_key: 用过它的其它任务数}``。"""
    tasks: dict[str, set] = {}
    for key, use, same in _other_uses(manifest):
        if not same:
            tasks.setdefault(key, set()).add(str(use.get("task") or use.get("draft") or ""))
    return {key: len(names) for key, names in tasks.items()}


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


def record(manifest: dict[str, Any], segments: Iterable[dict[str, Any]], *,
           reserved: bool = False) -> dict[str, Any]:
    """落本草稿用到的镜头；``reserved`` 是选镜后、成稿前的占位。"""
    raw = str(manifest.get("usage_ledger_path") or "").strip()
    task, draft = _identity(manifest)
    if not raw or not draft:
        return {"recorded": 0, "path": raw}
    keys = segment_keys(segments)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    use = {"task": task, "draft": draft, "at": now, **({"reserved": True} if reserved else {})}

    def mutate(data):
        data = _drop_draft(data, draft, reserved_only=reserved)
        shots = data.setdefault("shots", {})
        for key in keys:
            uses = shots.setdefault(key, [])
            uses.append(dict(use))
            del uses[:-MAX_USES_PER_SHOT]
        if not reserved:
            data.setdefault("drafts", {})[draft] = {"task": task, "at": now, "shots": keys}
        data["schema"] = "shot-usage-v1"
        return data

    data = locked_json_update(Path(raw), mutate, default={})
    out = {"recorded": len(keys), "path": raw}
    if not reserved:
        out["overlap"] = overlap_for(data, draft)
    return out


def release(manifest: dict[str, Any]) -> None:
    """撤掉本草稿没成稿的占位；已正式记录的不动。"""
    raw = str(manifest.get("usage_ledger_path") or "").strip()
    _task, draft = _identity(manifest)
    if not raw or not draft:
        return

    def mutate(data):
        return _drop_draft(data, draft, reserved_only=True)

    locked_json_update(Path(raw), mutate, default={})


def _drop_draft(data: Any, draft: str, *, reserved_only: bool = False) -> dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    shots = data.setdefault("shots", {})
    for key in list(shots):
        shots[key] = [u for u in shots[key] or [] if not (
            isinstance(u, dict) and u.get("draft") == draft
            and (u.get("reserved") or not reserved_only))]
        if not shots[key]:
            shots.pop(key)
    return data


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
