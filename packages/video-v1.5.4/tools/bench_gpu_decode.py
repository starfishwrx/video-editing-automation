"""GPU 与 CPU 解码对比：用生产里的稳定性检测、场景探测和抽帧函数各跑一遍。

    python tools/bench_gpu_decode.py <视频文件或目录...> [--limit N] [--out report.json]
    python tools/bench_gpu_decode.py <视频...> --ladder 1,3,6,9,12 [--ladder-cpu] [--out ...]

每个视频、每一步先跑 GPU（不设 JY_GPU_DISABLE），再跑 CPU（JY_GPU_DISABLE=1），
记录耗时、加速比和 ``gpu_runtime`` 统计出的解码结果。只读源视频；抽出的帧放临时目录。

``--ladder`` 是并发阶梯：每一档同时跑 N 路整段运动曲线解码（不经槽位限流），
记录总耗时、吞吐（视频秒/墙钟秒）、显存峰值、解码器占用峰值和回退 CPU 的次数，
用来确定 JY_GPU_DECODE_SLOTS 的拐点。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "doubao-jianying-orchestrator"))

from orchestrator import gpu_runtime, shot_analyzer, stability, video_decode  # noqa: E402
from orchestrator.platform_env import find_ffmpeg  # noqa: E402

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mkv", ".webm"}
STEPS = ("stability", "scene", "frames")


def collect_videos(inputs: list[str], limit: int, min_mb: float) -> list[Path]:
    """展开目录，按（文件名, 大小）去重，大文件优先。"""
    found: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            found.extend(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
        elif path.is_file():
            found.append(path)
    seen: set[tuple[str, int]] = set()
    unique: list[Path] = []
    for path in sorted(found, key=lambda p: p.stat().st_size, reverse=True):
        size = path.stat().st_size
        key = (path.name.lower(), size)
        if key in seen or size < min_mb * 1024 * 1024:
            continue
        seen.add(key)
        unique.append(path)
    return unique[:limit] if limit > 0 else unique


def probe(ffprobe: str, video: Path) -> dict:
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,pix_fmt,r_frame_rate:format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    stream = (data.get("streams") or [{}])[0]
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except ValueError:
        duration = 0.0
    return {
        "codec": stream.get("codec_name"),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "pix_fmt": stream.get("pix_fmt"),
        "fps": stream.get("r_frame_rate"),
        "duration_s": round(duration, 3),
        "size_mb": round(video.stat().st_size / 1024 / 1024, 1),
    }


def run_step(step: str, video: Path, duration: float, ffmpeg: str, work: Path) -> dict:
    if step == "stability":
        points = stability.motion_curve(video, ffmpeg=ffmpeg)
        frozen = stability.freeze_ranges(video, ffmpeg=ffmpeg)
        black = stability.has_black(video, 0.0, max(0.1, duration), ffmpeg=ffmpeg)
        return {"motion_points": len(points), "freeze_ranges": len(frozen), "black": black}
    if step == "scene":
        cuts = shot_analyzer.detect_scene_boundaries(video, ffmpeg=ffmpeg)
        return {"scene_cuts": len(cuts)}
    mid = shot_analyzer.extract_frame(video, duration / 2.0, work / "mid.jpg", ffmpeg=ffmpeg)
    last = shot_analyzer.extract_frame(video, max(0.0, duration - 0.15), work / "last.jpg",
                                       ffmpeg=ffmpeg)
    return {"mid_ok": mid, "last_ok": last}


def timed(step: str, video: Path, duration: float, ffmpeg: str, *, gpu: bool) -> dict:
    if gpu:
        os.environ.pop("JY_GPU_DISABLE", None)
    else:
        os.environ["JY_GPU_DISABLE"] = "1"
    gpu_runtime.reset_stats()
    with tempfile.TemporaryDirectory(prefix="bench_gpu_") as raw:
        started = time.perf_counter()
        error = None
        try:
            detail = run_step(step, video, duration, ffmpeg, Path(raw))
        except Exception as exc:  # noqa: BLE001
            detail, error = {}, f"{type(exc).__name__}: {exc}"
        seconds = time.perf_counter() - started
    snap = gpu_runtime.snapshot()["decode"]
    outcomes = {k: v for k, v in snap["outcomes"].items() if v}
    return {"seconds": round(seconds, 3), "outcomes": outcomes,
            "gpu_hit_rate": snap["gpu_hit_rate"], "detail": detail, "error": error}


def warm_cache(video: Path) -> None:
    with video.open("rb") as fh:
        while fh.read(8 * 1024 * 1024):
            pass


class GpuSampler:
    """后台每 0.5 秒读一次 nvidia-smi，记录显存占用和解码器占用的峰值。"""

    def __init__(self) -> None:
        self.mem_used: list[int] = []
        self.decoder: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                proc = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used,utilization.decoder",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5)
                first = (proc.stdout or "").strip().splitlines()[0]
                mem, dec = (int(x.strip()) for x in first.split(",")[:2])
                self.mem_used.append(mem)
                self.decoder.append(dec)
            except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                pass
            self._stop.wait(0.5)

    def __enter__(self) -> "GpuSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


def ladder_level(videos: list[Path], durations: dict, n: int, ffmpeg: str, *, gpu: bool) -> dict:
    if gpu:
        os.environ.pop("JY_GPU_DISABLE", None)
    else:
        os.environ["JY_GPU_DISABLE"] = "1"
    gpu_runtime.reset_stats()
    jobs = [videos[i % len(videos)] for i in range(n)]
    errors: list[str] = []
    per_job: list[float] = []

    def one(video: Path) -> None:
        started = time.perf_counter()
        try:
            stability.motion_curve(video, ffmpeg=ffmpeg)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
        per_job.append(time.perf_counter() - started)

    with GpuSampler() as sampler:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(one, jobs))
        wall = time.perf_counter() - started
    snap = gpu_runtime.snapshot()["decode"]
    video_seconds = sum(durations.get(v, 0.0) for v in jobs)
    return {
        "concurrency": n,
        "backend": "gpu" if gpu else "cpu",
        "wall_s": round(wall, 2),
        "per_job_median_s": round(statistics.median(per_job), 2) if per_job else None,
        "throughput_x": round(video_seconds / wall, 2) if wall > 0 else None,
        "vram_peak_mb": max(sampler.mem_used) if sampler.mem_used else None,
        "decoder_peak_pct": max(sampler.decoder) if sampler.decoder else None,
        "outcomes": {k: v for k, v in snap["outcomes"].items() if v},
        "gpu_hit_rate": snap["gpu_hit_rate"],
        "errors": errors[:3],
    }


def run_ladder(args, videos: list[Path], ffmpeg: str, ffprobe: str) -> int:
    levels = [int(x) for x in args.ladder.split(",") if x.strip().isdigit() and int(x) > 0]
    durations = {}
    for video in videos:
        durations[video] = probe(ffprobe, video).get("duration_s") or 0.0
        warm_cache(video)
    saved = {k: os.environ.get(k) for k in ("JY_GPU_DISABLE", "JY_GPU_DECODE_SLOTS",
                                            "JY_GPU_DECODE_CODECS")}
    os.environ["JY_GPU_DECODE_SLOTS"] = "0"
    os.environ["JY_GPU_DECODE_CODECS"] = "*"
    rows = []
    try:
        backends = [True, False] if args.ladder_cpu else [True]
        for gpu in backends:
            for n in levels:
                row = ladder_level(videos, durations, n, ffmpeg, gpu=gpu)
                rows.append(row)
                print(f"{row['backend']} n={n:<3} wall={row['wall_s']:>7.2f}s "
                      f"throughput={row['throughput_x']}x vram_peak={row['vram_peak_mb']}MB "
                      f"dec_peak={row['decoder_peak_pct']}% outcomes={row['outcomes']} "
                      f"errors={len(row['errors'])}", flush=True)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "ladder",
        "ffmpeg": ffmpeg,
        "videos": [{"file": str(v), "duration_s": durations[v]} for v in videos],
        "free_vram_mb_before": gpu_runtime.query_free_vram_mb(),
        "levels": rows,
    }
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--min-mb", type=float, default=0.0)
    parser.add_argument("--steps", default=",".join(STEPS))
    parser.add_argument("--out")
    parser.add_argument("--ladder", help="并发阶梯，例如 1,3,6,9,12")
    parser.add_argument("--ladder-cpu", action="store_true", help="阶梯同时跑一遍 CPU 作对照")
    args = parser.parse_args(argv)

    ffmpeg, ffprobe = find_ffmpeg()
    if not ffmpeg or not ffprobe:
        print("找不到 ffmpeg/ffprobe", file=sys.stderr)
        return 2
    ffmpeg, ffprobe = str(ffmpeg), str(ffprobe)
    steps = [s.strip() for s in args.steps.split(",") if s.strip() in STEPS]
    videos = collect_videos(args.inputs, args.limit, args.min_mb)
    if not videos:
        print("没有找到视频", file=sys.stderr)
        return 2
    gpu_runtime.set_call_log(None)
    if args.ladder:
        return run_ladder(args, videos, ffmpeg, ffprobe)
    original_disable = os.environ.get("JY_GPU_DISABLE")
    rows = []
    try:
        for video in videos:
            info = probe(ffprobe, video)
            warm_cache(video)
            entry = {"file": str(video), **info, "steps": {}}
            for step in steps:
                gpu = timed(step, video, info.get("duration_s") or 0.0, ffmpeg, gpu=True)
                cpu = timed(step, video, info.get("duration_s") or 0.0, ffmpeg, gpu=False)
                speedup = round(cpu["seconds"] / gpu["seconds"], 2) if gpu["seconds"] > 0 else None
                entry["steps"][step] = {"gpu": gpu, "cpu": cpu, "speedup": speedup}
            rows.append(entry)
            print(f"done {video.name}", flush=True)
    finally:
        if original_disable is None:
            os.environ.pop("JY_GPU_DISABLE", None)
        else:
            os.environ["JY_GPU_DISABLE"] = original_disable

    speedups = {step: [r["steps"][step]["speedup"] for r in rows
                       if r["steps"][step]["speedup"] is not None] for step in steps}
    every = [v for values in speedups.values() for v in values]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ffmpeg": ffmpeg,
        "hwaccels": sorted(video_decode.probe_hwaccels(ffmpeg) or []),
        "free_vram_mb": gpu_runtime.query_free_vram_mb(),
        "steps": steps,
        "videos": rows,
        "median_speedup": {step: (round(statistics.median(v), 2) if v else None)
                           for step, v in speedups.items()},
        "overall_median_speedup": round(statistics.median(every), 2) if every else None,
    }

    header = f"{'file':<28}{'codec':<7}{'res':<11}{'dur':>6}  " + "  ".join(
        f"{s + ' gpu/cpu/x':<24}" for s in steps)
    print(header)
    for r in rows:
        res = f"{r.get('width')}x{r.get('height')}"
        cells = []
        for step in steps:
            item = r["steps"][step]
            flag = ",".join(item["gpu"]["outcomes"]) or "-"
            cells.append(f"{item['gpu']['seconds']:>6.2f}/{item['cpu']['seconds']:>6.2f}/"
                         f"{item['speedup'] or 0:>4.2f} {flag:<6}")
        print(f"{Path(r['file']).name[:27]:<28}{str(r.get('codec'))[:6]:<7}{res:<11}"
              f"{r.get('duration_s') or 0:>6.1f}  " + "  ".join(cells))
    print("median speedup:", report["median_speedup"], "overall:", report["overall_median_speedup"])

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
