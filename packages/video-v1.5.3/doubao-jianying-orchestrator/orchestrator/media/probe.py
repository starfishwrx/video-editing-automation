"""Read real stream facts, including exact PCM narration length."""
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import wave


def probe_media(path, *, ffprobe="ffprobe"):
    path = Path(path).resolve()
    stat = path.stat()
    proc = subprocess.run([str(ffprobe), "-v", "error", "-show_streams", "-show_format",
                           "-of", "json", str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, check=True)
    raw = json.loads(proc.stdout)
    streams = raw.get("streams", [])
    result = {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
              "streams": streams, "audio": any(s.get("codec_type") == "audio" for s in streams)}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    selected = video or next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = selected.get("duration") or raw.get("format", {}).get("duration")
    if duration is None:
        raise ValueError("MEDIA_DURATION_UNKNOWN")
    duration = Fraction(str(duration))
    if not selected.get("duration"):
        origin = Fraction(str(raw.get("format", {}).get("start_time") or 0))
        if origin > 0:
            duration -= origin
    result["duration_us"] = round(duration * 1_000_000)
    if not video and path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as audio:
                result["duration_us"] = round(Fraction(audio.getnframes(), audio.getframerate()) * 1_000_000)
        except wave.Error:
            pass
    if result["duration_us"] <= 0:
        raise ValueError("MEDIA_DURATION_INVALID")
    if video:
        fps = Fraction(video.get("avg_frame_rate") or "0/1")
        result.update(width=int(video["width"]), height=int(video["height"]),
                      fps_num=fps.numerator, fps_den=fps.denominator)
    if (path.stat().st_size, path.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        raise ValueError("MEDIA_CHANGED_DURING_PROBE")
    return result
