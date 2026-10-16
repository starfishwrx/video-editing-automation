"""稳定窗口单次解码与旧版三次解码的 A/B 对照：选出的窗口必须完全一致。

    python tools/ab_stability_single_pass.py <视频文件或目录...> [--limit N] [--out report.json]

每个视频取若干（目标时长, 首选入点）组合，分别以 JY_STABILITY_SINGLE_PASS=0 / 1 调用
生产里的 ``stability.find_stable_window``，比较入点、均值、峰值和 reason，并记录耗时。
默认 JY_GPU_DISABLE=1，排除解码器差异；加 --gpu 则按生产路由走。只读源视频。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "doubao-jianying-orchestrator"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_gpu_decode import collect_videos, probe, warm_cache  # noqa: E402
from orchestrator import stability  # noqa: E402
from orchestrator.platform_env import find_ffmpeg  # noqa: E402

TARGETS = (2.0, 3.5)


def cases(duration: float, dense: float = 0.0) -> list[tuple[float, float]]:
    out = []
    for target in TARGETS:
        if duration < target + 0.2:
            continue
        starts = {0.0, round(duration * 0.4, 2), round(max(0.0, duration - target - 0.4), 2)}
        if dense > 0:
            n = int((duration - target) / dense)
            starts.update(round(i * dense, 2) for i in range(n + 1))
        out.extend((target, s) for s in sorted(starts))
    return out


def run(video: Path, target: float, start: float, duration: float, ffmpeg: str,
        single: bool) -> tuple[dict, float]:
    os.environ["JY_STABILITY_SINGLE_PASS"] = "1" if single else "0"
    started = time.perf_counter()
    window = stability.find_stable_window(video, target, requested_start=start,
                                          video_duration=duration, ffmpeg=ffmpeg)
    seconds = time.perf_counter() - started
    return {"source_start": round(window.source_start, 3),
            "mean": round(window.mean_motion, 3), "peak": round(window.peak_motion, 3),
            "reason": window.reason}, seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--min-mb", type=float, default=0.0)
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--dense", type=float, default=0.0,
                        help="额外每隔 N 秒取一个首选入点，用于扫黑场/静止边界")
    parser.add_argument("--out")
    args = parser.parse_args(argv)

    ffmpeg, ffprobe = find_ffmpeg()
    if not ffmpeg or not ffprobe:
        print("找不到 ffmpeg/ffprobe", file=sys.stderr)
        return 2
    ffmpeg, ffprobe = str(ffmpeg), str(ffprobe)
    videos = collect_videos(args.inputs, args.limit, args.min_mb)
    if not videos:
        print("没有找到视频", file=sys.stderr)
        return 2
    saved = {k: os.environ.get(k) for k in ("JY_GPU_DISABLE", "JY_STABILITY_SINGLE_PASS")}
    if not args.gpu:
        os.environ["JY_GPU_DISABLE"] = "1"
    rows, mismatches, legacy_t, single_t = [], 0, [], []
    try:
        for video in videos:
            info = probe(ffprobe, video)
            duration = info.get("duration_s") or 0.0
            warm_cache(video)
            for target, start in cases(duration, args.dense):
                old, t_old = run(video, target, start, duration, ffmpeg, single=False)
                new, t_new = run(video, target, start, duration, ffmpeg, single=True)
                same = old == new
                mismatches += 0 if same else 1
                legacy_t.append(t_old)
                single_t.append(t_new)
                rows.append({"file": str(video), "codec": info.get("codec"), "target": target,
                             "requested_start": start, "same": same, "legacy": old,
                             "single": new, "legacy_s": round(t_old, 3),
                             "single_s": round(t_new, 3)})
                flag = "OK  " if same else "DIFF"
                print(f"{flag} {video.name[:30]:<31}{str(info.get('codec')):<6} t={target:<4} "
                      f"s={start:<6} {t_old:6.2f}s -> {t_new:6.2f}s", flush=True)
                if not same:
                    print(f"     legacy={old}\n     single={new}", flush=True)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    speedups = [a / b for a, b in zip(legacy_t, single_t) if b > 0]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "gpu": args.gpu,
        "cases": len(rows),
        "mismatches": mismatches,
        "legacy_total_s": round(sum(legacy_t), 2),
        "single_total_s": round(sum(single_t), 2),
        "median_speedup": round(statistics.median(speedups), 2) if speedups else None,
        "rows": rows,
    }
    print(f"cases={len(rows)} mismatches={mismatches} legacy={report['legacy_total_s']}s "
          f"single={report['single_total_s']}s median_speedup={report['median_speedup']}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report: {out}")
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
