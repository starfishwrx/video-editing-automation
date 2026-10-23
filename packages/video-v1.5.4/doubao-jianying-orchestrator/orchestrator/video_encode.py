"""生产路径上的 H.264 视频编码参数。

只负责「把画面编成剪映能打开的 H.264」。抽帧、场景探测、静帧和黑场
走 ``video_decode``。音频响度、ffprobe 时长不要走这里，避免改掉它们的参数。

优先 NVIDIA ``h264_nvenc``，其次 AMD ``h264_amf``、Intel ``h264_qsv``。
编码器没编进 ffmpeg，或运行时打不开 GPU，就回退 ``libx264``，并打印一行
当前实际用的编码器。再不行才用 Windows MediaFoundation / mpeg4，避免
剪映自带的精简 ffmpeg 把整条生产打停。

GPU 编码那一次要占 ``gpu_runtime.gpu_slot("encode")``；``JY_GPU_DISABLE``、显存不足或
槽位排队超时就跳过 GPU，直接试 libx264。实际编码器和耗时记进 ``gpu_runtime``。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import NamedTuple, Optional

from . import gpu_runtime

# 探测行形如 `` V....D h264_nvenc           NVIDIA ...``。
# 说明行 `` V..... = Video`` 的第二列不是编码器名，不会命中。
_ENCODER_LINE = re.compile(r"^\s*[A-Z.]{6}\s+([A-Za-z0-9_]+)\s+")

GPU_ENCODERS = ("h264_nvenc", "h264_amf", "h264_qsv")
# libx264 是明确的 CPU 回退；后两个只在 libx264 也没有时保住占位片。
CPU_FALLBACKS = ("libx264", "h264_mf", "mpeg4")


class EncodeAttempt(NamedTuple):
    ffmpeg: str
    encoder: str
    args: list[str]


_probe_cache: dict[str, set[str] | None] = {}
_announced: set[str] = set()
_lock = threading.Lock()


def clear_encoder_cache() -> None:
    """测试用：清掉探测缓存和已打印的编码器说明。"""
    with _lock:
        _probe_cache.clear()
        _announced.clear()


def parse_encoder_names(text: str) -> set[str]:
    names: set[str] = set()
    for line in (text or "").splitlines():
        match = _ENCODER_LINE.match(line)
        if match:
            names.add(match.group(1))
    return names


def probe_video_encoders(ffmpeg: str) -> set[str] | None:
    """返回 ffmpeg 声明支持的编码器名。探测失败时返回 None，调用方再按顺序试。"""
    key = str(ffmpeg or "ffmpeg")
    with _lock:
        if key in _probe_cache:
            return _probe_cache[key]
    found: set[str] | None
    try:
        proc = subprocess.run(
            [key, "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        found = None
    else:
        found = parse_encoder_names(proc.stdout or "")
        if not found:
            found = None
    with _lock:
        _probe_cache[key] = found
    return found


def h264_codec_args(name: str) -> list[str]:
    """剪映可打开的 H.264 参数。nvenc 不用 libx264 的 -preset ultrafast / -crf。"""
    if name == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc",
            "-pix_fmt", "yuv420p",
            "-preset", "p4",
            "-rc", "vbr",
            "-cq", "23",
            "-b:v", "8M",
            "-maxrate", "12M",
            "-bufsize", "16M",
            "-profile:v", "high",
            "-tag:v", "avc1",
        ]
    if name == "h264_amf":
        return [
            "-c:v", "h264_amf",
            "-pix_fmt", "yuv420p",
            "-quality", "balanced",
            "-rc", "vbr_peak",
            "-b:v", "8M",
            "-maxrate", "12M",
            "-profile:v", "high",
            "-tag:v", "avc1",
        ]
    if name == "h264_qsv":
        return [
            "-c:v", "h264_qsv",
            "-pix_fmt", "yuv420p",
            "-preset", "medium",
            "-b:v", "8M",
            "-maxrate", "12M",
            "-profile:v", "high",
            "-tag:v", "avc1",
        ]
    if name == "libx264":
        return [
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "veryfast",
            "-crf", "20",
            "-profile:v", "high",
            "-tag:v", "avc1",
        ]
    if name == "h264_mf":
        return ["-c:v", "h264_mf", "-pix_fmt", "yuv420p", "-tag:v", "avc1"]
    if name == "mpeg4":
        return ["-c:v", "mpeg4", "-q:v", "5", "-pix_fmt", "yuv420p"]
    raise ValueError(f"未知视频编码器: {name}")


def find_libx264_ffmpeg(primary: str) -> str | None:
    """当前 ffmpeg 没有 libx264 时，另找一份有 libx264 的可执行文件。

    剪映捆绑的 ffmpeg 经常带 NVENC、不带 libx264。CPU 回退必须换到
    PATH / WinGet 那份，否则 GPU 一失败就只剩 mpeg4。
    """
    try:
        primary_resolved = str(Path(primary).resolve()) if Path(primary).is_file() else str(primary)
    except OSError:
        primary_resolved = str(primary)
    candidates: list[str] = []
    env = os.environ.get("FFMPEG_BIN", "").strip()
    if env:
        candidates.append(env)
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(str(Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"))
    candidates.append(r"C:\ffmpeg\bin\ffmpeg.exe")
    seen: set[str] = set()
    for cand in candidates:
        if not cand or not Path(cand).is_file():
            continue
        try:
            resolved = str(Path(cand).resolve())
        except OSError:
            continue
        if resolved in seen or resolved == primary_resolved:
            continue
        seen.add(resolved)
        names = probe_video_encoders(resolved)
        if names and "libx264" in names:
            return resolved
    return None


def encoder_plan(ffmpeg: str) -> list[EncodeAttempt]:
    """第一个是首选。GPU 运行失败后再试 libx264，最后才是 h264_mf / mpeg4。"""
    primary = str(ffmpeg or "ffmpeg")
    available = probe_video_encoders(primary)
    names: list[str] = []
    if available is None:
        names.append(GPU_ENCODERS[0])
        names.extend(CPU_FALLBACKS)
    else:
        for name in GPU_ENCODERS:
            if name in available:
                names.append(name)
                break
        for name in CPU_FALLBACKS:
            if name in available and name not in names:
                names.append(name)
        if not names:
            names.append("libx264")
    attempts = [EncodeAttempt(primary, name, h264_codec_args(name)) for name in names]
    if not any(item.encoder == "libx264" for item in attempts):
        alternate = find_libx264_ffmpeg(primary)
        if alternate:
            insert_at = 1 if attempts and attempts[0].encoder in GPU_ENCODERS else 0
            attempts.insert(
                insert_at,
                EncodeAttempt(alternate, "libx264", h264_codec_args("libx264")),
            )
    return attempts


def _announce(name: str, *, fallback_from: str | None) -> None:
    if name in GPU_ENCODERS and not fallback_from:
        message = f"视频编码：{name}（GPU）"
    elif name == "libx264" and fallback_from:
        message = f"视频编码：libx264（CPU 回退，{fallback_from} 不可用）"
    elif name == "libx264":
        message = "视频编码：libx264（CPU，未发现 h264_nvenc/h264_amf/h264_qsv）"
    elif fallback_from:
        message = f"视频编码：{name}（回退，{fallback_from} 不可用）"
    else:
        message = f"视频编码：{name}"
    with _lock:
        if message in _announced:
            return
        _announced.add(message)
    print(message, flush=True)


def _discard(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def run_h264_encode(
    ffmpeg: str,
    leading: list[str],
    output: str | Path,
    *,
    trailing: list[str] | None = None,
    timeout: float | None = 180,
) -> tuple[str, subprocess.CompletedProcess]:
    """按 encoder_plan 依次编码。成功打印一行编码器；全部失败才抛错。

    ``leading`` 是视频编码器参数之前的参数（含 ``-y`` 和输入），
    ``trailing`` 是其后的参数（例如音频 ``-c:a``），输出路径由本函数追加。
    """
    dest = Path(output)
    tail = list(trailing or [])
    plan = encoder_plan(str(ffmpeg or "ffmpeg"))
    preferred = plan[0].encoder
    errors: list[str] = []
    started = time.monotonic()
    waited = 0.0
    skipped: Optional[str] = None
    # 被跳过的 GPU 尝试排到队尾：CPU 全部失败时才不占槽位再试一次。
    queue: list[tuple[EncodeAttempt, bool]] = [(item, False) for item in plan]
    for item, forced in queue:
        name, codec_args, ffmpeg_bin = item.encoder, item.args, item.ffmpeg
        cmd = [ffmpeg_bin, *leading, *codec_args, *tail,
               "-movflags", "+faststart", str(dest)]
        if name in GPU_ENCODERS and not forced:
            skip = _gpu_skip_reason()
            if skip:
                skipped = skip
                errors.append(f"{name}: skipped ({skip})")
                if skip != "gpu_disabled":
                    queue.append((item, True))
                continue
            with gpu_runtime.gpu_slot("encode") as handle:
                waited += handle.waited
                if not handle.acquired:
                    skipped = "slot_timeout"
                    errors.append(f"{name}: skipped (slot_timeout)")
                    queue.append((item, True))
                    continue
                proc, error = _attempt(name, cmd, timeout)
        else:
            proc, error = _attempt(name, cmd, timeout)
        if proc is not None and proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            fallback_from = preferred if name != preferred else None
            _announce(name, fallback_from=fallback_from)
            gpu_runtime.record_encode(name, fallback_from,
                                      time.monotonic() - started - waited,
                                      slot_wait=waited, outcome=skipped)
            return name, proc
        errors.append(error or f"{name}: exit {proc.returncode} "
                      f"{(proc.stderr or proc.stdout or '')[-500:]}".strip())
        _discard(dest)
    gpu_runtime.record_encode("failed", preferred, time.monotonic() - started - waited,
                              slot_wait=waited, outcome="failed")
    raise RuntimeError("视频编码失败：" + " | ".join(errors))


def _gpu_skip_reason() -> Optional[str]:
    if gpu_runtime.gpu_disabled():
        return "gpu_disabled"
    if not gpu_runtime.vram_ok():
        return "vram_low"
    return None


def _attempt(name: str, cmd: list[str], timeout: float | None
             ) -> tuple[Optional[subprocess.CompletedProcess], Optional[str]]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or "")[-500:]
        elif isinstance(exc, subprocess.TimeoutExpired):
            detail = "timeout"
        return None, f"{name}: {exc} {detail}".strip()
    return proc, None
