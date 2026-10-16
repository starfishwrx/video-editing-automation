"""GPU 调用的进程级统计、跨进程槽位和显存余量判断。

``video_decode`` / ``video_encode`` 在每次 ffmpeg 调用后记一笔，引擎在构建结束时
把 ``snapshot()`` 写进事件和结果。GPU 只是加速：槽位排队超时、显存不足、
``JY_GPU_DISABLE=1`` 都直接走 CPU；统计与日志本身出错也不能让媒体任务失败。

环境变量：
- ``JY_GPU_DISABLE``：1/true 时所有解码、编码都走 CPU。
- ``JY_GPU_DECODE_SLOTS``：同时做 CUDA 解码的 ffmpeg 进程数（跨进程），默认 6。
  RTX 3070 8GB 实测 4K HEVC：6 路吞吐已到顶（8.3x，12 路 8.5x），每路约 550MB 显存。
  NVDEC 在 GeForce 上没有会话数上限，真正的保护是显存门槛。旧变量
  ``JY_GPU_SLOTS`` 仍被接受，作为解码槽位的取值。
- ``JY_GPU_ENCODE_SLOTS``：同时做 GPU 编码的进程数，默认 6。GeForce 驱动限制
  NVENC 并发会话数，给剪映预览/导出留出余量。<=0 表示不限。
- ``JY_GPU_SLOT_TIMEOUT``：排队上限秒数，默认 120，超时走 CPU。
- ``JY_GPU_MIN_FREE_MB``：nvidia-smi 报告的空闲显存低于该值时走 CPU，默认 768。
- ``JY_GPU_DECODE_CODECS``：走 CUDA 解码的视频编码格式，逗号分隔，默认 ``hevc``；
  ``*`` 表示不限。2026-10-10 实测：HEVC 稳定性检测 GPU 约快 1.4 倍；H.264 在
  ``-threads 1`` 下 GPU 只有 CPU 的 0.5–0.95 倍。
- ``JY_GPU_SINGLE_FRAME``：1 时单帧抽取也尝试 CUDA。默认关闭：CUDA 初始化的
  固定开销大于单帧收益（实测 0.7–1.0 倍）。
- ``JY_GPU_CALL_LOG``：逐次调用 JSONL 路径；``set_call_log`` 显式设置时优先。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .proc_slots import SlotHandle, env_float, env_int, slot

DECODE_OUTCOMES = ("gpu_ok", "gpu_init_fallback", "gpu_fail_rerun",
                   "cpu_only", "slot_timeout", "vram_low", "codec_cpu", "single_frame_cpu")
GPU_ENCODERS = ("h264_nvenc", "h264_amf", "h264_qsv")
VRAM_CACHE_S = 5.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_lock = threading.Lock()
_call_log: Optional[Path] = None
_vram_cache: Optional[tuple[float, Optional[int]]] = None


def _empty_stats() -> dict:
    return {
        "decode_outcomes": {name: 0 for name in DECODE_OUTCOMES},
        "decode_seconds": {"cuda": 0.0, "cpu": 0.0},
        "encode_encoders": {},
        "encode_outcomes": {},
        "encode_seconds": {},
        "encode_fallbacks": 0,
        "slot_wait_s": 0.0,
    }


_stats = _empty_stats()


def reset_stats() -> None:
    """清空本进程的统计和显存缓存（每次构建开始、测试用）。"""
    global _stats, _vram_cache
    with _lock:
        _stats = _empty_stats()
        _vram_cache = None
        _codec_cache.clear()


def gpu_disabled() -> bool:
    return os.environ.get("JY_GPU_DISABLE", "").strip().lower() in {"1", "true", "yes", "on"}


def set_call_log(path: str | Path | None) -> None:
    """设置逐次调用的 JSONL 文件；None 表示改回只看 ``JY_GPU_CALL_LOG``。"""
    global _call_log
    with _lock:
        _call_log = Path(path) if path else None


def _call_log_path() -> Optional[Path]:
    if _call_log is not None:
        return _call_log
    raw = os.environ.get("JY_GPU_CALL_LOG", "").strip()
    return Path(raw) if raw else None


def _append_call(entry: dict) -> None:
    try:
        path = _call_log_path()
        if path is None:
            return
        entry = {"ts": datetime.now().isoformat(timespec="milliseconds"), **entry}
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001
        pass


def record_decode(backend: str, outcome: str, seconds: float, *,
                  slot_wait: float = 0.0) -> None:
    """记一次解码。``backend`` 是实际出画面的一侧：cuda 或 cpu。"""
    try:
        seconds = max(0.0, float(seconds))
        wait = max(0.0, float(slot_wait or 0.0))
        with _lock:
            outcomes = _stats["decode_outcomes"]
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            per_backend = _stats["decode_seconds"]
            per_backend[backend] = per_backend.get(backend, 0.0) + seconds
            _stats["slot_wait_s"] += wait
    except Exception:  # noqa: BLE001
        return
    _append_call({"kind": "decode", "backend": backend, "outcome": outcome,
                  "seconds": round(seconds, 3), "slot_wait": round(wait, 3)})


def record_encode(encoder: str, fallback_from: Optional[str], seconds: float, *,
                  slot_wait: float = 0.0, outcome: Optional[str] = None) -> None:
    """记一次编码。``outcome`` 默认按是否回退取 ok / fallback；跳过 GPU 时写原因。"""
    try:
        seconds = max(0.0, float(seconds))
        wait = max(0.0, float(slot_wait or 0.0))
        result = outcome or ("fallback" if fallback_from else "ok")
        with _lock:
            encoders = _stats["encode_encoders"]
            encoders[encoder] = encoders.get(encoder, 0) + 1
            outcomes = _stats["encode_outcomes"]
            outcomes[result] = outcomes.get(result, 0) + 1
            per_encoder = _stats["encode_seconds"]
            per_encoder[encoder] = per_encoder.get(encoder, 0.0) + seconds
            if fallback_from:
                _stats["encode_fallbacks"] += 1
            _stats["slot_wait_s"] += wait
    except Exception:  # noqa: BLE001
        return
    _append_call({"kind": "encode", "encoder": encoder, "fallback_from": fallback_from,
                  "outcome": result, "seconds": round(seconds, 3),
                  "slot_wait": round(wait, 3)})


def _rate(hits: int, total: int) -> Optional[float]:
    return round(hits / total, 3) if total else None


def snapshot() -> dict:
    """统计快照：解码按结果计数、按后端累计秒数，编码按编码器计数，以及 GPU 命中率。"""
    with _lock:
        stats = json.loads(json.dumps(_stats))
    decode_total = sum(stats["decode_outcomes"].values())
    encode_total = sum(stats["encode_encoders"].values())
    encode_gpu = sum(n for name, n in stats["encode_encoders"].items() if name in GPU_ENCODERS)
    return {
        "decode": {
            "calls": decode_total,
            "outcomes": stats["decode_outcomes"],
            "seconds": {k: round(v, 3) for k, v in stats["decode_seconds"].items()},
            "gpu_hit_rate": _rate(stats["decode_outcomes"].get("gpu_ok", 0), decode_total),
        },
        "encode": {
            "calls": encode_total,
            "encoders": stats["encode_encoders"],
            "outcomes": stats["encode_outcomes"],
            "fallbacks": stats["encode_fallbacks"],
            "seconds": {k: round(v, 3) for k, v in stats["encode_seconds"].items()},
            "gpu_hit_rate": _rate(encode_gpu, encode_total),
        },
        "slot_wait_s": round(stats["slot_wait_s"], 3),
        "gpu_disabled": gpu_disabled(),
        "decode_slots": slot_capacity("decode"),
        "encode_slots": slot_capacity("encode"),
        "decode_codecs": sorted(decode_codecs() or {"*"}),
        "min_free_mb": env_int("JY_GPU_MIN_FREE_MB", 768),
    }


def slot_capacity(kind: str) -> int:
    if kind == "encode":
        return int(_env_signed("JY_GPU_ENCODE_SLOTS", 6))
    if os.environ.get("JY_GPU_DECODE_SLOTS", "").strip():
        return int(_env_signed("JY_GPU_DECODE_SLOTS", 6))
    return int(_env_signed("JY_GPU_SLOTS", 6))


def _env_signed(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@contextmanager
def gpu_slot(kind: str = "decode") -> Iterator[SlotHandle]:
    """占一个跨进程 GPU 槽位（解码、编码分池）。``acquired`` 为 False 时调用方走 CPU。

    槽位目录本身打不开时不限流放行：GPU 失败另有 CPU 回退兜底。
    """
    name = "gpu_encode" if kind == "encode" else "gpu_decode"
    stack = ExitStack()
    try:
        handle = stack.enter_context(slot(
            name, slot_capacity(kind),
            timeout=env_float("JY_GPU_SLOT_TIMEOUT", 120.0)))
    except OSError:
        handle = SlotHandle(name, -1, 0.0)
    with stack:
        yield handle


def decode_codecs() -> Optional[set[str]]:
    """允许 CUDA 解码的编码格式；None 表示不限。"""
    raw = os.environ.get("JY_GPU_DECODE_CODECS", "hevc").strip().lower()
    if raw in {"*", "all", "any"}:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def single_frame_gpu() -> bool:
    return os.environ.get("JY_GPU_SINGLE_FRAME", "").strip().lower() in {"1", "true", "yes", "on"}


_codec_cache: dict[str, Optional[str]] = {}


def _ffprobe_for(ffmpeg: str) -> Optional[str]:
    path = Path(str(ffmpeg))
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = path.with_name(name)
        if candidate.is_file():
            return str(candidate)
    return None


def probe_video_codec(ffmpeg: str, source: str) -> Optional[str]:
    """首条视频流的编码名（如 hevc、h264），按路径缓存。查不到返回 None。"""
    key = str(source)
    with _lock:
        if key in _codec_cache:
            return _codec_cache[key]
    codec: Optional[str] = None
    try:
        ffprobe = _ffprobe_for(ffmpeg)
        if ffprobe:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", key],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=20, creationflags=_NO_WINDOW)
            name = str(proc.stdout or "").strip().splitlines()
            codec = name[0].strip().lower() if name and name[0].strip() else None
        else:
            proc = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", key],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=20, creationflags=_NO_WINDOW)
            for line in str(proc.stderr or "").splitlines():
                if "Video:" in line:
                    codec = line.split("Video:", 1)[1].strip().split()[0].strip(",").lower()
                    break
    except Exception:  # noqa: BLE001
        codec = None
    with _lock:
        _codec_cache[key] = codec
    return codec


def clear_codec_cache() -> None:
    with _lock:
        _codec_cache.clear()


def decode_route(cmd: list[str]) -> Optional[str]:
    """按命令内容判断是否该直接走 CPU：返回 CPU 原因（outcome 名），或 None 表示可试 GPU。

    编码格式未知时保持试 GPU，避免 HEVC 因探针失败退回 CPU。
    """
    if not single_frame_gpu():
        for i, item in enumerate(cmd[:-1]):
            if item in ("-frames:v", "-vframes") and str(cmd[i + 1]).strip() == "1":
                return "single_frame_cpu"
    allowed = decode_codecs()
    if allowed is None or "-i" not in cmd:
        return None
    source = cmd[cmd.index("-i") + 1] if cmd.index("-i") + 1 < len(cmd) else ""
    codec = probe_video_codec(str(cmd[0]), str(source)) if source else None
    if codec is not None and codec not in allowed:
        return "codec_cpu"
    return None


def query_free_vram_mb() -> Optional[int]:
    """第一块 NVIDIA 卡的空闲显存（MB），缓存 5 秒。查不到返回 None。"""
    global _vram_cache
    now = time.monotonic()
    with _lock:
        if _vram_cache is not None and now - _vram_cache[0] < VRAM_CACHE_S:
            return _vram_cache[1]
    free: Optional[int] = None
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, creationflags=_NO_WINDOW,
        )
        if proc.returncode == 0:
            for line in str(proc.stdout or "").splitlines():
                item = line.strip()
                if item.isdigit():
                    free = int(item)
                    break
    except Exception:  # noqa: BLE001
        free = None
    with _lock:
        _vram_cache = (time.monotonic(), free)
    return free


def vram_ok() -> bool:
    """空闲显存够用返回 True；nvidia-smi 不可用或读不懂时不拦（未知即放行）。"""
    free = query_free_vram_mb()
    if free is None:
        return True
    return free >= env_int("JY_GPU_MIN_FREE_MB", 768)
