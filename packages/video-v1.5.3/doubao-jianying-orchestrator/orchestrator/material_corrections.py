"""素材纠错层：核对过的镜头特征覆盖临时分析缓存里的错误描述。

临时分析（``shot_candidates.json``）是模型看抽帧写的，会认错动作、漏读包装字样；它被多轮复用，
错的描述也会一直被复用。纠错文件按素材原片记录核对结果，选镜前叠到分析结果上：

- 用原片签名（文件大小 + 首尾各 1 MiB 的 SHA-256）认素材，签名对不上的条目不用并记下来；
- 只改条目里写明的字段，原值留在 ``correction.original`` 里，旧的分析文件不改；
- AI 复核和人工抽检分开记（``review.ai_check`` / ``review.human_check``），没有人工抽检就是 ``null``。

纠错文件由任务的 ``material_corrections_file``（一个或多个路径）指定。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

SCHEMA = "material_corrections.v1"
_CHUNK = 1 << 20


def signature(path: str | os.PathLike[str]) -> dict[str, Any]:
    """素材身份：文件大小 + 首尾各 1 MiB 的 SHA-256（NAS 上整片算哈希太慢）。"""
    p = Path(path)
    size = p.stat().st_size
    digest = hashlib.sha256()
    with p.open("rb") as handle:
        digest.update(handle.read(_CHUNK))
        if size > _CHUNK:
            handle.seek(max(_CHUNK, size - _CHUNK))
            digest.update(handle.read(_CHUNK))
    return {"size": size, "head_tail_sha256": digest.hexdigest()}


def _paths(manifest: dict[str, Any]) -> list[Path]:
    value = manifest.get("material_corrections_file")
    items = value if isinstance(value, list) else [value] if value else []
    return [Path(str(item)).expanduser() for item in items if str(item or "").strip()]


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: schema 不是 {SCHEMA}")
    return data


def apply(manifest: dict[str, Any], analysis: dict[str, Any],
          report_dir: Path | None = None) -> dict[str, Any]:
    """把纠错叠到分析结果的镜头上，返回新的分析结果（不改入参）。"""
    files = _paths(manifest)
    if not files or not isinstance(analysis.get("shots"), list):
        return analysis
    report: dict[str, Any] = {"files": [], "applied": [], "skipped": []}
    entries: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for path in files:
        try:
            data = load(path)
        except (OSError, ValueError) as exc:
            report["files"].append({"path": str(path), "error": str(exc)[:300]})
            continue
        report["files"].append({"path": str(path), "version": data.get("version")})
        for entry in data.get("entries") or []:
            if isinstance(entry, dict) and entry.get("file"):
                entries.setdefault(str(entry["file"]).lower(), []).append((entry, str(data.get("version") or "")))
    signatures: dict[str, dict[str, Any] | None] = {}
    shots = []
    for shot in analysis["shots"]:
        video = str((shot or {}).get("video") or "")
        matches = entries.get(Path(video).name.lower()) if video else None
        if not matches:
            shots.append(shot)
            continue
        if video not in signatures:
            try:
                signatures[video] = signature(video)
            except OSError:
                signatures[video] = None
        actual = signatures[video]
        updated = dict(shot)
        for entry, version in matches:
            expected = entry.get("signature") or {}
            if not actual or any(actual.get(k) != v for k, v in expected.items()):
                report["skipped"].append({"shot_id": shot.get("shot_id"), "file": entry.get("file"),
                                          "reason": "SIGNATURE_MISMATCH" if actual else "FILE_UNREADABLE"})
                continue
            fields = entry.get("fields") or {}
            original = {key: shot.get(key) for key in fields}
            updated.update(fields)
            updated["correction"] = {
                "version": version, "file": entry.get("file"), "original": original,
                "action_interval": entry.get("action_interval"), "supports": entry.get("supports"),
                "not_supported": entry.get("not_supported"), "review": entry.get("review"),
            }
            report["applied"].append({"shot_id": shot.get("shot_id"), "file": entry.get("file"),
                                      "fields": sorted(fields), "version": version})
        shots.append(updated)
    out = {**analysis, "shots": shots, "material_corrections": report}
    if report_dir is not None:
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        (Path(report_dir) / "material_corrections_applied.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
