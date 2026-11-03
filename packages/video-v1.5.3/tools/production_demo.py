"""Reproducible synthetic fixed-selection demo. No model, TTS, draft or business access."""
import argparse
import json
from pathlib import Path
import subprocess

from tools.production import main as production_main


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    parser.add_argument("--width", type=int, default=360)
    args = parser.parse_args(argv)
    from orchestrator.runtime.production_env import production_environment
    env = production_environment(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
    if not env["ok"]:
        parser.error(",".join(env["issues"]))
    root = args.output.resolve()
    if root.exists():
        parser.error("Output directory must be new")
    root.mkdir(parents=True)
    ffmpeg = env["tools"]["ffmpeg"]
    def run(options):
        subprocess.run([ffmpeg, "-v", "error", "-nostdin", *options], capture_output=True, check=True, timeout=90)
    video = root / "synthetic.mkv"
    run(["-f", "lavfi", "-i", "testsrc2=size=360x640:rate=60:duration=2.5", "-f", "lavfi", "-i",
         "sine=frequency=880:sample_rate=48000:duration=2.5", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(video)])
    voices = []
    for i, (duration, frequency) in enumerate(((.8, 440), (53614 / 48000, 550))):
        path = root / f"synthetic-audio-{i}.wav"
        run(["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={duration:.12f}",
             "-ac", "2", "-c:a", "pcm_s16le", str(path)])
        voices.append(path)
    bgm = root / "synthetic-bgm.wav"
    run(["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=2.5", "-ac", "2", "-c:a", "pcm_s16le", str(bgm)])
    manifest = {"task_id": "synthetic-fixed-selection", "delivery_mode": "formal", "width": 1080,
        "height": 1920, "fps": 30, "bgm": {"path": str(bgm), "gain": .15, "fade_in": .1, "fade_out": .1},
        "segments": [
            {"video": str(video), "source_start": 0, "audio": str(voices[0]), "text": "合成素材固定选镜",
             "keep_original_audio": True, "original_gain": .1},
            {"video": str(video), "source_start": .8, "audio": str(voices[1]), "text": "独立音轨与真实末帧",
             "protected_intervals": [{"interval_id": "synthetic-result", "source_start_us": 1_900_000,
                                      "source_end_us": 1_916_958, "kind": "result", "min_output_frames": 1}],
             "keep_original_audio": True, "original_gain": .1}]}
    input_path = root / "fixed-selection.json"
    input_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return production_main(["preview", "--input", str(input_path), "--report-dir", str(root / "production"),
                            "--width", str(args.width), "--ffmpeg", ffmpeg, "--ffprobe", env["tools"]["ffprobe"]])


if __name__ == "__main__":
    raise SystemExit(main())
