"""Source-PTS frames and motion cues for bounded action review, without AI."""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .video_decode import run_ffmpeg_decode
from ..runtime.runtime_policy import media_slot


def identity(source: str | Path) -> dict:
    path = Path(source).resolve()
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _key(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def _save(path: Path, row) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def probe(source: str | Path, directory: Path, *, ffprobe="ffprobe") -> dict:
    before = identity(source)
    path = Path(directory) / ("pts-" + _key(before) + ".json")
    if path.is_file():
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("source_identity") == before and row.get("frames"):
            return row
    proc = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height,time_base,avg_frame_rate:frame=best_effort_timestamp", "-of", "json", str(source)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=True)
    data = json.loads(proc.stdout)
    stream = data["streams"][0]
    unit = Fraction(stream["time_base"])
    raw = [round(int(f["best_effort_timestamp"]) * unit * 1_000_000) for f in data["frames"]]
    if len(raw) < 2 or any(a >= b for a, b in zip(raw, raw[1:])):
        raise ValueError("ACTION_SOURCE_PTS_NOT_STRICTLY_INCREASING")
    origin = raw[0]
    frames = [{"index": i, "pts_us": value - origin} for i, value in enumerate(raw)]
    if before != identity(source):
        raise ValueError("ACTION_SOURCE_CHANGED_DURING_PROBE")
    row = {"schema": "action-source-pts-v1", "source_identity": before,
           "stream": stream, "timestamp_origin_us": origin, "frames": frames,
           "source_end_us": frames[-1]["pts_us"] + raw[-1] - raw[-2]}
    _save(path, row)
    return row


def extract(source: str | Path, metadata: dict, indices: list[int], directory: Path, *, ffmpeg="ffmpeg", width: int = 360) -> list[dict]:
    """Select decoder frame indices and verify the observed PTS, never relabel requested times."""
    if metadata["source_identity"] != identity(source):
        raise ValueError("ACTION_SOURCE_IDENTITY_CHANGED")
    if type(width) is not int or width < 32 or width % 2:
        raise ValueError("ACTION_EXTRACTION_WIDTH_INVALID")
    if (not isinstance(indices, list) or not indices
            or any(type(i) is not int or not 0 <= i < len(metadata["frames"]) for i in indices)
            or indices != sorted(set(indices))):
        raise ValueError("ACTION_FRAME_INDICES_INVALID")
    root = Path(directory) / ("frames-" + _key([metadata["source_identity"], {"width": width, "protocol": "source-index-v1"}]))
    root.mkdir(parents=True, exist_ok=True)
    rows = [dict(metadata["frames"][i], path=str(root / ("%06d.jpg" % i))) for i in indices]
    missing = [r for r in rows if not Path(r["path"]).is_file()]
    if missing:
        import tempfile
        selected = "+".join("eq(n\\,%d)" % r["index"] for r in missing)
        with tempfile.TemporaryDirectory(prefix="extract-", dir=root) as temp:
            pattern = str(Path(temp) / "%06d.jpg")
            with media_slot(timeout=600) as handle:
                if not handle.acquired:
                    raise ValueError("ACTION_MEDIA_SLOT_TIMEOUT")
                proc = run_ffmpeg_decode([ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-copyts", "-i", str(source),
                    "-an", "-vf", "select='" + selected + "',scale=" + str(width) + ":-2,showinfo", "-fps_mode", "vfr",
                    "-frames:v", str(len(missing)), "-q:v", "2", "-y", pattern],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
            paths = sorted(Path(temp).glob("*.jpg"))
            observed = [round(float(v) * 1e6) - metadata["timestamp_origin_us"]
                        for v in re.findall(r"pts_time:\s*([-0-9.]+)", proc.stderr or "")]
            if proc.returncode or len(paths) != len(missing) or len(observed) != len(missing):
                raise ValueError("ACTION_FRAME_EXTRACTION_INCOMPLETE")
            for row, path, pts in zip(missing, paths, observed):
                if abs(pts - row["pts_us"]) > 10:
                    raise ValueError("ACTION_FRAME_PTS_MISMATCH")
                Path(row["path"]).write_bytes(path.read_bytes())
    if metadata["source_identity"] != identity(source):
        raise ValueError("ACTION_SOURCE_CHANGED_DURING_EXTRACTION")
    return rows


def motion_cues(frames: list[dict]) -> dict:
    """Measure background-compensated ROI motion; this is never semantic proof."""
    import cv2
    import numpy as np
    rows, previous = [], None
    for frame in frames:
        encoded = np.fromfile(frame["path"], dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError("ACTION_FRAME_UNREADABLE")
        image = cv2.resize(image, (144, 256))
        if previous is not None:
            flow = cv2.calcOpticalFlowFarneback(previous, image, None, .5, 3, 15, 3, 5, 1.2, 0)
            background = np.concatenate((flow[:40].reshape(-1, 2), flow[-25:].reshape(-1, 2)))
            camera = np.median(background, axis=0)
            region = (flow - camera)[45:225, 15:130]
            magnitude = np.linalg.norm(region, axis=2)
            rows.append({"index": frame["index"], "pts_us": frame["pts_us"],
                         "motion_q75": float(np.quantile(magnitude, .75)),
                         "motion_q90": float(np.quantile(magnitude, .9)),
                         "camera_magnitude": float(np.linalg.norm(camera))})
        previous = image
    return {"semantic_evidence": False, "method": "ROI_Farneback_background_median", "rows": rows}


def detail_windows(cues: dict, *, margin_us: int = 200000, width_us: int = 700000) -> dict:
    """Suggest two bounded review regions; motion never labels an action or its result."""
    rows = cues.get("rows")
    if not isinstance(rows, list) or not rows or margin_us < 0 or width_us <= 0:
        raise ValueError("ACTION_MOTION_CUES_INVALID")
    values = sorted(r["motion_q90"] for r in rows)
    threshold = max(.5, values[len(values) // 2] * .7)
    active = [r for r in rows if r["motion_q90"] >= threshold]
    if not active:
        return {"semantic_evidence": False, "windows": [], "threshold": threshold}
    low, high = rows[0]["pts_us"], rows[-1]["pts_us"]
    start, end = active[0]["pts_us"], active[-1]["pts_us"]
    windows = [{"purpose": "motion_onset_detail", "start_us": max(low, start - margin_us),
                "end_us": min(high, start - margin_us + width_us)},
               {"purpose": "motion_offset_detail", "start_us": max(low, end + margin_us - width_us),
                "end_us": min(high, end + margin_us)}]
    return {"semantic_evidence": False, "threshold": threshold, "windows": windows,
            "active_start_us": start, "active_end_us": end}
