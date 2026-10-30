"""生产路径上的视频解码：优先 NVIDIA CUDA，失败再回退 CPU。

静帧、黑场、运动曲线、场景探测和抽帧都要先把画面解出来。
编码仍走 ``video_encode``。容器时长探针和音频不要走这里。

剪映这份 ffmpeg 列出了 cuda。4K HEVC 若用默认线程数，NVDEC
解码面会超过 32，初始化失败后 ffmpeg 仍以退出码 0 把这一次改在
CPU 上解完。GPU 尝试必须带 ``-threads 1``。stderr 里若出现
硬件解码初始化失败，结果已经是 CPU 解出来的，不再重解；
只有进程真的失败时，才去掉硬件参数再跑一遍。

GPU 尝试前先看 ``gpu_runtime``：``JY_GPU_DISABLE``、显存不足、跨进程槽位
排队超时都直接走 CPU。槽位只在 GPU 那一次期间占用，CPU 重跑前已释放。
每次调用的结果记进 ``gpu_runtime`` 统计。
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

from . import gpu_runtime

_HWACCEL_LINE = re.compile(r"^[a-z0-9_]+$")
# 这些字样表示本次没有留在 GPU 上。退出码仍常常是 0，滤镜结果可用。
_HWACCEL_FAILED = (
    "Failed setup for format cuda",
    "Failed setup for format d3d11",
    "hwaccel initialisation returned error",
    "CUDA_ERROR",
    "Static surface pool size exceeded",
)

_probe_cache: dict[str, set[str] | None] = {}
_announced: set[str] = set()
_lock = threading.Lock()


def clear_decode_cache() -> None:
    """测试用：清掉硬件解码探测缓存和已打印的说明。"""
    with _lock:
        _probe_cache.clear()
        _announced.clear()


def parse_hwaccel_names(text: str) -> set[str]:
    names: set[str] = set()
    for line in (text or "").splitlines():
        item = line.strip()
        if _HWACCEL_LINE.fullmatch(item):
            names.add(item)
    return names


def probe_hwaccels(ffmpeg: str) -> set[str] | None:
    """返回 ffmpeg 声明的硬件加速名。探测失败时返回 None。"""
    key = str(ffmpeg or "ffmpeg")
    with _lock:
        if key in _probe_cache:
            return _probe_cache[key]
    found: set[str] | None
    try:
        proc = subprocess.run(
            [key, "-hide_banner", "-hwaccels"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        found = None
    else:
        found = parse_hwaccel_names(proc.stdout or "")
        if not found:
            found = None
    with _lock:
        _probe_cache[key] = found
    return found


def gpu_decode_args(ffmpeg: str) -> list[str]:
    """本机验证过的 GPU 解码参数。没有 cuda 就不加，避免用未验证的 d3d11va。"""
    available = probe_hwaccels(ffmpeg)
    if available is None or "cuda" not in available:
        return []
    return ["-threads", "1", "-hwaccel", "cuda"]


def with_gpu_decode(cmd: list[str], extra: list[str] | None = None) -> list[str]:
    """把 GPU 解码参数放在输入文件之前。已经带 -hwaccel 的命令原样返回。"""
    if not cmd or "-i" not in cmd or "-hwaccel" in cmd:
        return list(cmd)
    prefix = list(extra if extra is not None else gpu_decode_args(str(cmd[0])))
    if not prefix:
        return list(cmd)
    return [cmd[0], *prefix, *cmd[1:]]


def _stderr_text(proc: subprocess.CompletedProcess) -> str:
    err = proc.stderr or ""
    if isinstance(err, bytes):
        return err.decode("utf-8", errors="replace")
    return str(err)


def hwaccel_failed(stderr: str) -> bool:
    text = stderr or ""
    return any(marker in text for marker in _HWACCEL_FAILED)


def _announce(message: str) -> None:
    with _lock:
        if message in _announced:
            return
        _announced.add(message)
    print(message, flush=True)


def run_ffmpeg_decode(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """先按 GPU 解码跑。硬件初始化失败但进程成功时，沿用这次 CPU 结果。

    进程失败才去掉 ``-hwaccel`` 再跑原命令。``check=True`` 只看最终这次。
    """
    check = bool(kwargs.pop("check", False))
    original = list(cmd)
    gpu_cmd = with_gpu_decode(original)
    started = time.monotonic()
    skip = None
    if gpu_cmd == original:
        skip = "cpu_only"
    elif gpu_runtime.gpu_disabled():
        skip = "cpu_only"
        _announce("视频解码：JY_GPU_DISABLE=1，走 CPU")
    elif (route := gpu_runtime.decode_route(original)) is not None:
        skip = route
        _announce("视频解码：单帧抽取走 CPU" if route == "single_frame_cpu"
                  else "视频解码：非 HEVC 素材走 CPU（实测 GPU 更慢）")
    elif not gpu_runtime.vram_ok():
        skip = "vram_low"
        _announce("视频解码：空闲显存不足，本次走 CPU")
    if skip:
        proc = subprocess.run(original, **kwargs)
        gpu_runtime.record_decode("cpu", skip, time.monotonic() - started)
        return _checked(proc, check)

    proc = None
    with gpu_runtime.gpu_slot("decode") as handle:
        waited = handle.waited
        if handle.acquired:
            proc = subprocess.run(gpu_cmd, **kwargs)
    if proc is None:
        _announce("视频解码：GPU 槽位排队超时，本次走 CPU")
        proc = subprocess.run(original, **kwargs)
        gpu_runtime.record_decode("cpu", "slot_timeout",
                                  time.monotonic() - started - waited, slot_wait=waited)
        return _checked(proc, check)
    err = _stderr_text(proc)
    if proc.returncode == 0 and hwaccel_failed(err):
        _announce("视频解码：cuda 初始化失败，本次已在 CPU 上完成")
        backend, outcome = "cpu", "gpu_init_fallback"
    elif proc.returncode != 0:
        _announce("视频解码：cuda 失败，回退 CPU")
        proc = subprocess.run(original, **kwargs)
        backend, outcome = "cpu", "gpu_fail_rerun"
    else:
        _announce("视频解码：cuda hwaccel（GPU，threads=1）")
        backend, outcome = "cuda", "gpu_ok"
    gpu_runtime.record_decode(backend, outcome,
                              time.monotonic() - started - waited, slot_wait=waited)
    return _checked(proc, check)


def _checked(proc: subprocess.CompletedProcess, check: bool) -> subprocess.CompletedProcess:
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, proc.stdout, proc.stderr)
    return proc
