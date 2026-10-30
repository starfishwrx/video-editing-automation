"""Media probing, scene detection and accurately timed frame extraction.

This adapter owns only FFmpeg work. Selection and visual services may inject
execution functions without making the media layer depend on their modules.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable, Iterable

from .video_decode import run_ffmpeg_decode

_PTS_TIME_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def probe_duration(path: Path, *, ffprobe: str | None = None,
                   ffmpeg: str | None = None, runner: Callable | None = None) -> float:
    """Read duration using ffprobe, falling back to ffmpeg stderr parsing."""
    runner = runner or subprocess.run
    if ffprobe:
        try:
            proc = runner(
                [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, check=True)
            duration = _number(proc.stdout.strip())
            if duration > 0:
                return duration
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    command = str(ffmpeg or "ffmpeg")
    try:
        proc = runner([command, "-hide_banner", "-i", str(path)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30)
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
        if match:
            h, m, s = match.groups()
            return int(h) * 3600 + int(m) * 60 + float(s)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return 0.0


def detect_scene_boundaries(path: Path, *, ffmpeg: str | None = None,
                            threshold: float = 0.30, decoder: Callable | None = None) -> list[float]:
    """Return scene-change timestamps from ffmpeg's showinfo output."""
    decoder = decoder or run_ffmpeg_decode
    command = str(ffmpeg or "ffmpeg")
    filter_expr = f"select='gt(scene,{float(threshold):.3f})',showinfo"
    try:
        proc = decoder(
            [command, "-hide_banner", "-nostdin", "-i", str(path), "-vf", filter_expr,
             "-an", "-f", "null", "-"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({round(float(value), 3) for value in _PTS_TIME_RE.findall(proc.stderr or "")
                   if float(value) > 0})


def extract_frame(path: Path, timestamp: float, output: Path, *,
                  ffmpeg: str | None = None, decoder: Callable | None = None) -> bool:
    decoder = decoder or run_ffmpeg_decode
    command = str(ffmpeg or "ffmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        decoder([command, "-y", "-hide_banner", "-loglevel", "error",
                           "-ss", f"{max(0.0, timestamp):.3f}", "-i", str(path),
                           "-frames:v", "1", "-q:v", "2", str(output)],
                          capture_output=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return output.is_file() and output.stat().st_size > 0


def batch_extract_enabled() -> bool:
    return str(os.environ.get("JY_FRAME_BATCH", "1")).strip() != "0"


def extract_frames_batch(
    path: Path,
    requests: Iterable[tuple[float, Path]],
    *,
    ffmpeg: str | None = None,
    timeout: float | None = None,
    match_tol_s: float = 0.20,
    decoder: Callable | None = None,
    single_frame: Callable | None = None,
    batch_enabled: bool | None = None,
) -> list[bool]:
    """一次解码抽出多个时刻的 jpeg。

    ``requests`` 是 ``(timestamp_sec, output_path)`` 列表，返回与之等长的成功标志。
    用 select 在每个目标时刻取第一帧（包括零时刻和可变帧率），
    输入 seek 到首个目标之前，只解码覆盖目标的范围；showinfo 的相对时间
    加回 seek 偏移后对齐源时间。对不上的时刻回退单点 ``extract_frame``。

    关闭：``JY_FRAME_BATCH=0``。单帧请求直接走 ``extract_frame``。
    ``-fps_mode vfr`` 失败且工作目录没有输出时，改试旧版 ``-vsync vfr``，
    仍失败再逐帧回退。
    """
    decoder = decoder or run_ffmpeg_decode
    if single_frame is None:
        def single_frame(source, timestamp, output, *, ffmpeg=None):
            return extract_frame(source, timestamp, output, ffmpeg=ffmpeg, decoder=decoder)
    if batch_enabled is None:
        batch_enabled = batch_extract_enabled()
    items = [(round(max(0.0, float(ts)), 3), Path(out)) for ts, out in requests]
    if not items:
        return []
    if len(items) == 1 or not batch_enabled:
        return [single_frame(path, ts, out, ffmpeg=ffmpeg) for ts, out in items]

    # De-dupe identical timestamps while keeping first output as canonical.
    unique: list[tuple[float, Path]] = []
    seen: set[float] = set()
    for ts, out in items:
        out.parent.mkdir(parents=True, exist_ok=True)
        if ts in seen:
            continue
        seen.add(ts)
        unique.append((ts, out))
    unique.sort(key=lambda row: row[0])

    command = str(ffmpeg or "ffmpeg")
    # Accurate input seeking discards preceding keyframe preroll. FFmpeg then
    # reports timestamps relative to this seek; retain the source-time offset.
    seek = max(0.0, unique[0][0] - 0.25)
    span = unique[-1][0] - seek + max(0.25, match_tol_s)
    select = "+".join(
        f"(isnan(prev_pts)+lt(prev_pts*TB\\,{ts - seek:.6f}))*gte(pts*TB\\,{ts - seek:.6f})"
        for ts, _ in unique
    )
    # Separate simultaneous requests even when their first output stem agrees.
    with tempfile.TemporaryDirectory(prefix=".batch_", dir=unique[0][1].parent) as work_name:
        work = Path(work_name)
        try:
            pattern = work / "f_%04d.jpg"
            wait = float(timeout) if timeout is not None else max(60.0, 12.0 * len(unique))
            # FFmpeg 9 dropped ``-vsync``; ``-fps_mode vfr`` is the replacement.
            # Older builds still accept ``-vsync`` — try modern flag first.
            base_cmd = [command, "-y", "-hide_banner", "-nostdin",
                        "-ss", f"{seek:.6f}", "-t", f"{span:.6f}",
                        "-i", str(path), "-an", "-frames:v", str(len(unique)),
                        "-vf", f"select='{select}',showinfo"]
            proc = None
            last_exc: Exception | None = None
            for vfr_flag in (["-fps_mode", "vfr"], ["-vsync", "vfr"]):
                # 上一次失败留下的半成品不能算「已经抽出」，否则会跳过下一条旗标。
                for stale in list(work.glob("f_*.jpg")):
                    try:
                        stale.unlink()
                    except OSError:
                        pass
                try:
                    proc = decoder(
                        base_cmd + vfr_flag + ["-q:v", "2", str(pattern)],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=wait)
                    # Path.glob 返回生成器，布尔值恒为真。空目录必须用 any()
                    # 才会改试下一条 vfr 旗标（新 ffmpeg 的 -fps_mode，旧版的 -vsync）。
                    if proc.returncode == 0 or any(work.glob("f_*.jpg")):
                        break
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    last_exc = exc
                    proc = None
                    continue
            if proc is None:
                if last_exc is not None:
                    raise last_exc
                return [single_frame(path, ts, out, ffmpeg=ffmpeg) for ts, out in items]
        except (OSError, subprocess.SubprocessError, ValueError):
            return [single_frame(path, ts, out, ffmpeg=ffmpeg) for ts, out in items]

        written = sorted(work.glob("f_*.jpg"))
        pts = [float(v) + seek for v in _PTS_TIME_RE.findall(proc.stderr or "")]
        # A missing encoded file makes positional PTS pairing ambiguous.
        # Re-seek instead of attaching another frame to the wrong evidence time.
        produced = list(zip(pts, written)) if len(pts) == len(written) else []
        assigned: dict[float, Path] = {}
        for ts, _canonical in unique:
            best = None
            best_delta = None
            for pts_t, file_path in produced:
                # A nearby *earlier* frame is not evidence for this request.
                # Several targets may legitimately select the same VFR frame.
                delta = pts_t - ts
                if delta < -1e-6 or delta > match_tol_s:
                    continue
                if best_delta is None or delta < best_delta:
                    best = file_path
                    best_delta = delta
            if best is None:
                continue
            assigned[ts] = best

        # Map unique timestamps back onto every request (including duplicates).
        ok_flags: list[bool] = []
        for ts, out in items:
            src = assigned.get(ts)
            if src is None:
                ok_flags.append(single_frame(path, ts, out, ffmpeg=ffmpeg))
                continue
            try:
                if out.exists() and out.resolve() != src.resolve():
                    out.unlink()
                if src.resolve() != out.resolve():
                    # Copy rather than rename so duplicate timestamps can share source.
                    out.write_bytes(src.read_bytes())
                ok_flags.append(out.is_file() and out.stat().st_size > 0)
            except OSError:
                ok_flags.append(single_frame(path, ts, out, ffmpeg=ffmpeg))

        return ok_flags
