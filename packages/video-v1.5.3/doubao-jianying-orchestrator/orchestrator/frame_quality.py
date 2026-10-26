"""抽帧质量门：进视觉模型前拒绝黑场、过曝、纯色；模糊只作相对排序。

设计来自开源调研（findit-keyframe / framewise）：亮度均值 / 方差做硬门控，
Laplacian 锐度在桶内候选间择优。只用 Pillow，缺依赖时自动关闭。
开关：``JY_FRAME_QUALITY=0`` 关闭（只按抽帧成败，不跑硬门）。
候选全部硬拒时外层 ``ok=False``，调用方不得把这些帧送进接触表。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# v2：候选全部硬拒时外层 ok=False，接触表不再收下这些帧。
# 视觉 / 相位缓存键引用本字符串；改准入口径必须升版本，避免复用 v1 观察。
FRAME_QUALITY_VERSION = "frame-quality-v2"
DISABLE_ENV = "JY_FRAME_QUALITY"

# findit-keyframe 同族阈值：偏保守，避免误杀柔焦产品特写。
BLACK_MEAN = 15.0
BRIGHT_MEAN = 240.0
FLAT_STD = 5.0
# Laplacian 方差（Pillow Kernel，offset=128）。清晰棋盘约 3000，重糊约 <80。
# 模糊不做硬拒绝（电商产品片常柔焦）；只在已抽出的候选里当相对排序信号。
# 硬门只拦黑场 / 过曝 / 纯色，避免无意义地连发 FFmpeg 换帧。
BLUR_SHARPNESS = 80.0  # 仅用于标注 soft_blur；不触发 reject_reason
HARD_REJECTS = frozenset({"black", "overexposed", "flat", "unreadable", "extract_failed"})
_LAPLACIAN = None


def _laplacian_filter():
    global _LAPLACIAN
    if _LAPLACIAN is None:
        from PIL import ImageFilter  # type: ignore
        _LAPLACIAN = ImageFilter.Kernel(
            (3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)
    return _LAPLACIAN


def enabled() -> bool:
    return str(os.environ.get(DISABLE_ENV, "1")).strip() != "0"


def score(path: str | Path) -> dict[str, Any]:
    """返回均值、标准差、锐度、soft_blur，以及硬拒绝原因 black/overexposed/flat 或空。"""
    result: dict[str, Any] = {
        "mean": 0.0, "std": 0.0, "sharpness": 0.0, "reject_reason": "", "ok": False,
    }
    try:
        from PIL import Image, ImageStat  # type: ignore
    except ImportError:
        result["reject_reason"] = "no_pillow"
        return result
    try:
        with Image.open(path) as image:
            gray = image.convert("L")
            stats = ImageStat.Stat(gray)
            mean = float(stats.mean[0])
            std = float(stats.stddev[0])
            sharpness = float(ImageStat.Stat(gray.filter(_laplacian_filter())).var[0])
    except OSError:
        result["reject_reason"] = "unreadable"
        return result
    reason = ""
    if mean < BLACK_MEAN:
        reason = "black"
    elif mean > BRIGHT_MEAN:
        reason = "overexposed"
    elif std < FLAT_STD:
        reason = "flat"
    soft_blur = bool(sharpness < BLUR_SHARPNESS) and not reason
    result.update({
        "mean": round(mean, 2), "std": round(std, 2),
        "sharpness": round(sharpness, 2), "reject_reason": reason,
        "soft_blur": soft_blur, "ok": not reason,
    })
    return result


def alternate_times(center: float, start: float, end: float, *,
                    half_width: float) -> list[float]:
    """桶内邻近候选：中心 ±1/3 桶宽，夹在镜头窗口内，去重且保序。"""
    span = max(0.0, float(half_width))
    if span <= 1e-6:
        return []
    delta = span / 3.0
    lo, hi = float(start), float(end)
    candidates: list[float] = []
    for offset in (-delta, delta, -2.0 * delta, 2.0 * delta):
        t = round(min(hi, max(lo, center + offset)), 3)
        if abs(t - center) < 1e-3:
            continue
        if all(abs(t - kept) >= 1e-3 for kept in candidates):
            candidates.append(t)
    return candidates


def _hard_reject_reason(row: dict[str, Any]) -> str:
    reason = str((row.get("quality") or {}).get("reject_reason") or "")
    return reason if reason in HARD_REJECTS else ""


def pick_best(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """优先通过硬门的帧；同档取锐度最高。空列表返回 None。

    全部硬拒时仍返回锐度最高的一帧，供诊断 reject_reason。
    是否当成成功帧由 ``_finalize_choice`` 决定（外层 ok=False）。
    """
    usable = [c for c in candidates if c.get("path")]
    if not usable:
        return None

    passed = [c for c in usable if not _hard_reject_reason(c)]
    pool = passed or usable
    return max(pool, key=lambda c: float((c.get("quality") or {}).get("sharpness") or 0.0))


def _finalize_choice(
    center: float,
    output: Path,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    best = pick_best(candidates)
    if best is None:
        return {
            "path": "", "timestamp": center,
            "quality": {"reject_reason": "extract_failed", "ok": False},
            "replaced": False, "ok": False,
        }
    chosen_file = Path(best["file"])
    final_out = output
    if chosen_file.resolve() != output.resolve():
        try:
            if output.exists():
                output.unlink()
            chosen_file.replace(output)
        except OSError:
            final_out = chosen_file
        for other in candidates:
            other_file = Path(other["file"])
            if other_file.resolve() == final_out.resolve():
                continue
            try:
                if other_file.exists() and other_file.name != final_out.name:
                    other_file.unlink()
            except OSError:
                pass
    # 只看外层 ok 的调用方（视觉拼图、相位打点）不得把硬拒帧送进接触表。
    # 路径和质量仍留下，方便日志看到是黑场 / 过曝 / 纯色，而不是抽帧失败。
    admitted = not _hard_reject_reason(best)
    return {
        "path": str(final_out),
        "timestamp": float(best["timestamp"]),
        "quality": best.get("quality") or {},
        "replaced": abs(float(best["timestamp"]) - center) >= 1e-3,
        "ok": admitted,
    }


def select_frame(
    video: Path,
    center: float,
    output: Path,
    *,
    start: float,
    end: float,
    half_width: float,
    extract: Callable[..., bool],
    ffmpeg: Optional[str] = None,
) -> dict[str, Any]:
    """抽中心帧；不合格时在桶内补抽并选最佳。关闭质量门时只抽中心。"""
    return select_frames(
        video,
        [{"center": center, "output": output, "start": start, "end": end,
          "half_width": half_width}],
        extract=extract, ffmpeg=ffmpeg,
    )[0]


def select_frames(
    video: Path,
    jobs: Sequence[dict[str, Any]],
    *,
    extract: Callable[..., bool],
    extract_batch: Optional[Callable[..., list[bool]]] = None,
    ffmpeg: Optional[str] = None,
) -> list[dict[str, Any]]:
    """批量选帧：先一次抽出所有中心，硬门失败的再一次抽出邻域候选。

    每个 job 需要 ``center/output/start/end/half_width``。
    ``extract_batch(video, [(t, path), ...], ffmpeg=) -> list[bool]`` 可选；
    缺省时退回逐个 ``extract``。
    """
    if not jobs:
        return []

    def _batch(requests: list[tuple[float, Path]]) -> list[bool]:
        if not requests:
            return []
        if extract_batch is not None:
            try:
                return list(extract_batch(video, requests, ffmpeg=ffmpeg))
            except TypeError:
                return list(extract_batch(video, requests))
        return [extract(video, ts, out, ffmpeg=ffmpeg) for ts, out in requests]

    prepared: list[dict[str, Any]] = []
    primary_requests: list[tuple[float, Path]] = []
    for job in jobs:
        center = round(float(job["center"]), 3)
        output = Path(job["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        prepared.append({
            "center": center,
            "output": output,
            "start": float(job["start"]),
            "end": float(job["end"]),
            "half_width": float(job["half_width"]),
            "candidates": [],
        })
        primary_requests.append((center, output))

    primary_ok = _batch(primary_requests)
    results: list[dict[str, Any]] = [{} for _ in prepared]
    need_alt: list[int] = []

    for index, (row, ok) in enumerate(zip(prepared, primary_ok)):
        center, output = row["center"], row["output"]
        if not enabled():
            quality = score(output) if ok else {"reject_reason": "extract_failed", "ok": False}
            results[index] = {
                "path": str(output) if ok else "",
                "timestamp": center,
                "quality": quality,
                "replaced": False,
                "ok": bool(ok),
            }
            continue
        if ok:
            quality = score(output)
            row["candidates"].append({
                "path": str(output), "timestamp": center, "quality": quality, "file": output,
            })
            if quality.get("ok"):
                results[index] = {
                    "path": str(output), "timestamp": center, "quality": quality,
                    "replaced": False, "ok": True,
                }
                continue
        need_alt.append(index)

    alt_requests: list[tuple[float, Path]] = []
    alt_owners: list[tuple[int, float, Path]] = []
    for index in need_alt:
        row = prepared[index]
        for alt_i, alt in enumerate(
                alternate_times(row["center"], row["start"], row["end"],
                                half_width=row["half_width"]), 1):
            alt_path = row["output"].with_name(
                f"{row['output'].stem}_alt{alt_i}{row['output'].suffix}")
            alt_requests.append((alt, alt_path))
            alt_owners.append((index, alt, alt_path))

    if alt_requests:
        for (index, alt, alt_path), ok in zip(alt_owners, _batch(alt_requests)):
            if not ok:
                continue
            prepared[index]["candidates"].append({
                "path": str(alt_path), "timestamp": alt,
                "quality": score(alt_path), "file": alt_path,
            })

    for index in need_alt:
        row = prepared[index]
        results[index] = _finalize_choice(row["center"], row["output"], row["candidates"])
    return results
