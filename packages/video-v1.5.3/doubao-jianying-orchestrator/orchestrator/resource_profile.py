"""Bounded resource profiles that reduce contention with an interactive editor."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace

from .platform_env import is_jianying_running


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    analysis_workers: int
    tts_concurrency: int
    staging_workers: int
    process_priority: str


PROFILES = {
    "performance": ResourceProfile("performance", 2, 3, 2, "normal"),
    "background_friendly": ResourceProfile("background_friendly", 1, 1, 1, "below_normal"),
    "gpu_balanced": ResourceProfile("gpu_balanced", 2, 2, 1, "below_normal"),
}

# 剪映打开时选用的档位：阶段间让出调度、内存偏低时降为最低并发。
FRIENDLY_PROFILES = frozenset({"background_friendly", "gpu_balanced"})


def gpu_decode_available() -> bool:
    """CUDA 解码可用时，分析阶段的解码压力转到 GPU，可以放宽并发。"""
    if os.environ.get("JY_GPU_DISABLE", "").strip() == "1":
        return False
    try:
        from .platform_env import find_ffmpeg
        from .video_decode import gpu_decode_args
        ffmpeg, _ = find_ffmpeg()
        if not ffmpeg:
            return False
        return bool(gpu_decode_args(str(ffmpeg)))
    except Exception:
        return False


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return max(1, int(raw)) if raw else default
    except ValueError:
        return default


def choose_profile(manifest: dict) -> ResourceProfile:
    """JY_EXECUTION_PROFILE > manifest.execution_profile > 按剪映/GPU 状态自动选择。

    在线配音只占网络，不随 CPU 档位收紧，统一由 ``JY_TTS_CONCURRENCY``（默认 3）决定；
    跨进程总量另由 voice_tts 的 tts 槽位限制。CUDA 可用时分析阶段的解码在 GPU 上，
    非后台档的分析线程放宽到 ``JY_ANALYSIS_WORKERS``（默认 4），GPU 槽位与显存检查兜底。
    """
    requested = os.environ.get("JY_EXECUTION_PROFILE", "").strip().lower()
    if requested not in PROFILES:
        requested = str((manifest or {}).get("execution_profile", "")).strip().lower()
    gpu = None
    if requested not in PROFILES:
        if is_jianying_running():
            gpu = gpu_decode_available()
            requested = "gpu_balanced" if gpu else "background_friendly"
        else:
            requested = "performance"
    profile = PROFILES[requested]
    changes: dict = {"tts_concurrency": _env_int("JY_TTS_CONCURRENCY", 3)}
    if requested != "background_friendly":
        if gpu is None:
            gpu = gpu_decode_available()
        if gpu:
            changes["analysis_workers"] = _env_int("JY_ANALYSIS_WORKERS", 4)
    return replace(profile, **changes)


def apply_process_priority(profile: ResourceProfile) -> bool:
    """Best effort priority lowering; never fails a media task."""
    if profile.process_priority != "below_normal":
        return False
    try:
        if sys.platform.startswith("win"):
            import psutil  # type: ignore
            psutil.Process(os.getpid()).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            return True
        if hasattr(os, "nice"):
            os.nice(5)
            return True
    except Exception:
        return False
    return False
