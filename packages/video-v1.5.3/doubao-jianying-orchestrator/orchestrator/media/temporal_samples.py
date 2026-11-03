"""Numbered native-video samples mapped to actual normalized-source frames."""
from pathlib import Path
from fractions import Fraction
import json
import re

from .action_frames import identity, probe
from .video_encode import run_h264_encode


def prepare(original, directory, *, ffmpeg="ffmpeg", ffprobe="ffprobe", font_path):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False)
    before = identity(original)
    source = root / "normalized.mp4"
    run_h264_encode(ffmpeg, ["-v","error","-nostdin","-threads","1","-i",str(original),"-an",
        "-vf","fps=60:eof_action=pass,scale=540:960:force_original_aspect_ratio=decrease,pad=540:960:(ow-iw)/2:(oh-ih)/2",
        "-r","60","-fps_mode","cfr","-video_track_timescale","60000"], source,
        trailing=["-pix_fmt","yuv420p","-metadata:s:v:0","rotate=0"], timeout=300)
    if identity(original) != before:
        raise ValueError("EXPERIMENT_ORIGINAL_CHANGED_DURING_NORMALIZATION")
    metadata = probe(source, root, ffprobe=ffprobe)
    fps = Fraction(metadata["stream"]["avg_frame_rate"])
    if fps != 60 or any(abs(r["pts_us"] - round(i*1_000_000/60)) > 1
            for i, r in enumerate(metadata["frames"])):
        raise ValueError("EXPERIMENT_SAMPLES_REQUIRE_EXPLICIT_CFR60")
    frames = [{"frame_id": i, "source_frame": r["index"], "pts_us": r["pts_us"]}
              for i, r in enumerate(metadata["frames"][::6])]
    target = root / "numbered.mp4"
    if target.exists():
        raise ValueError("EXPERIMENT_USE_NEW_MEDIA_DIRECTORY")
    font = str(Path(font_path).resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    filters = ("select='not(mod(n\\,6))',showinfo,setpts=N/(10*TB),scale=360:-2,"
        f"drawtext=fontfile='{font}':text='frame %{{n}}':fontsize=24:fontcolor=white:"
        "box=1:boxcolor=black@0.85:x=8:y=8")
    _, proc = run_h264_encode(ffmpeg, ["-v", "info", "-nostdin", "-i", str(source), "-an", "-vf", filters,
        "-r", "10", "-fps_mode", "cfr"], target,
        trailing=["-pix_fmt", "yuv420p"], timeout=180)
    observed = [round(float(t)*1_000_000) for t in re.findall(r"pts_time:\s*([-0-9.]+)",proc.stderr or "")]
    if len(observed) != len(frames) or any(abs(t-r["pts_us"]) > 10 for t,r in zip(observed,frames)):
        raise ValueError("EXPERIMENT_SAMPLED_PTS_MISMATCH")
    output_metadata = probe(target,root,ffprobe=ffprobe)
    if len(output_metadata["frames"]) != len(frames):
        raise ValueError("EXPERIMENT_SAMPLED_FRAME_COUNT_MISMATCH")
    if identity(source) != metadata["source_identity"] or identity(original) != before:
        raise ValueError("EXPERIMENT_SOURCE_CHANGED_DURING_SAMPLING")
    result = {"schema": "numbered-temporal-samples-v2-owned-normalization", "original":before,
        "normalization":"owned autorotate CFR60 same source clock", "source": metadata["source_identity"],
        "video": identity(target), "requested_fps": 10, "frames": frames,
        "source_end_us": metadata["source_end_us"]}
    (root / "samples.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def validate(samples):
    if samples.get("schema") != "numbered-temporal-samples-v2-owned-normalization":
        raise ValueError("EXPERIMENT_UNBOUND_OLD_MEDIA")
    saved_path = Path(samples["video"]["path"]).parent / "samples.json"
    if json.loads(saved_path.read_text(encoding="utf-8")) != samples:
        raise ValueError("EXPERIMENT_MEDIA_PROVENANCE_CHANGED")
    for key in ("original","source","video"):
        if samples[key] != identity(samples[key]["path"]):
            raise ValueError("EXPERIMENT_BOUND_MEDIA_CHANGED")
    return samples


def guard_outputs(samples, output, suffixes=()):
    paths = [Path(samples[k]["path"]).resolve() for k in ("original","source","video")]
    paths.append(Path(samples["video"]["path"]).parent / "samples.json")
    guard_paths(paths, output, suffixes)


def guard_paths(paths, output, suffixes=()):
    output = Path(output)
    destinations = [output] + [output.with_suffix(s) for s in suffixes]
    for target in destinations:
        for source in paths:
            source = Path(source)
            if target.resolve() == source.resolve() or (target.exists() and source.exists() and target.samefile(source)):
                raise ValueError("EXPERIMENT_OUTPUT_COLLIDES_WITH_MEDIA")
