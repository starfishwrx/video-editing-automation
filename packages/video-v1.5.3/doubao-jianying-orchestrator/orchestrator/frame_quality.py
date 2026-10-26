"""抽帧质量门：进视觉模型前拒绝黑场、过曝、纯色；模糊只作相对排序。

设计来自开源调研（findit-keyframe / framewise）：亮度均值 / 方差做硬门控，
Laplacian 锐度在桶内候选间择优。只用 Pillow，缺依赖时自动关闭。
开关：``JY_FRAME_QUALITY=0`` 关闭。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

FRAME_QUALITY_VERSION = "frame-quality-v1"
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


def pick_best(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """优先通过硬门的帧；同档取锐度最高。空列表返回 None。"""
    usable = [c for c in candidates if c.get("path")]
    if not usable:
        return None

    def hard_rejected(row: dict[str, Any]) -> bool:
        reason = str((row.get("quality") or {}).get("reject_reason") or "")
        return reason in HARD_REJECTS

    passed = [c for c in usable if not hard_rejected(c)]
    pool = passed or usable
    return max(pool, key=lambda c: float((c.get("quality") or {}).get("sharpness") or 0.0))


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
    output.parent.mkdir(parents=True, exist_ok=True)
    center = round(float(center), 3)
    if not enabled():
        ok = extract(video, center, output, ffmpeg=ffmpeg)
        quality = score(output) if ok else {"reject_reason": "extract_failed", "ok": False}
        return {
            "path": str(output) if ok else "",
            "timestamp": center,
            "quality": quality,
            "replaced": False,
            "ok": ok,
        }

    candidates: list[dict[str, Any]] = []
    primary_ok = extract(video, center, output, ffmpeg=ffmpeg)
    if primary_ok:
        quality = score(output)
        candidates.append({
            "path": str(output), "timestamp": center, "quality": quality, "file": output,
        })
        if quality.get("ok"):
            return {
                "path": str(output), "timestamp": center, "quality": quality,
                "replaced": False, "ok": True,
            }

    for index, alt in enumerate(alternate_times(center, start, end, half_width=half_width), 1):
        alt_path = output.with_name(f"{output.stem}_alt{index}{output.suffix}")
        if not extract(video, alt, alt_path, ffmpeg=ffmpeg):
            continue
        candidates.append({
            "path": str(alt_path), "timestamp": alt, "quality": score(alt_path),
            "file": alt_path,
        })

    best = pick_best(candidates)
    if best is None:
        return {
            "path": "", "timestamp": center,
            "quality": {"reject_reason": "extract_failed", "ok": False},
            "replaced": False, "ok": False,
        }

    chosen_file = Path(best["file"])
    if chosen_file.resolve() != output.resolve():
        try:
            if output.exists():
                output.unlink()
            chosen_file.replace(output)
        except OSError:
            # 换名失败就继续用候选路径，调用方仍能读到图。
            output = chosen_file
        for other in candidates:
            other_file = Path(other["file"])
            if other_file.resolve() == output.resolve():
                continue
            try:
                if other_file.exists() and other_file.name != output.name:
                    other_file.unlink()
            except OSError:
                pass

    return {
        "path": str(output),
        "timestamp": float(best["timestamp"]),
        "quality": best.get("quality") or {},
        "replaced": abs(float(best["timestamp"]) - center) >= 1e-3,
        "ok": True,
    }
