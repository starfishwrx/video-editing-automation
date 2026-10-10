"""生产路径上的视频解码：优先 NVIDIA CUDA，失败再回退 CPU。

静帧、黑场、运动曲线、场景探测和抽帧都要先把画面解出来。
编码仍走 ``video_encode``。容器时长探针和音频不要走这里。

剪映这份 ffmpeg 列出了 cuda。4K HEVC 若用默认线程数，NVDEC
解码面会超过 32，初始化失败后 ffmpeg 仍以退出码 0 把这一次改在
CPU 上解完。GPU 尝试必须带 ``-threads 1``。stderr 里若出现
硬件解码初始化失败，结果已经是 CPU 解出来的，不再重解；
只有进程真的失败时，才去掉硬件参数再跑一遍。
"""
from __future__ import annotations

import re
import subprocess
import threading

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
    gpu_cmd = with_gpu_decode(list(cmd))
    used_gpu = gpu_cmd != list(cmd)
    try:
        proc = subprocess.run(gpu_cmd, **kwargs)
    except subprocess.TimeoutExpired:
        raise
    except OSError:
        raise
    err = _stderr_text(proc)
    if used_gpu and proc.returncode == 0 and hwaccel_failed(err):
        _announce("视频解码：cuda 初始化失败，本次已在 CPU 上完成")
    elif used_gpu and proc.returncode != 0:
        _announce("视频解码：cuda 失败，回退 CPU")
        proc = subprocess.run(list(cmd), **kwargs)
    elif used_gpu and proc.returncode == 0:
        _announce("视频解码：cuda hwaccel（GPU，threads=1）")
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, proc.stdout, proc.stderr)
    return proc
