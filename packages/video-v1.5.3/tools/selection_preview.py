"""Read-only FFmpeg selection previews; these are not Jianying native exports.

Only video windows may be overridden. Narration, BGM and text are read from the
same saved baseline timeline. No project file is written or changed.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import subprocess

US = 1_000_000


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("TIMELINE_OBJECT_REQUIRED")
    return value


def _number(value, name: str, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("INVALID_" + name)
    if value < 0 or (positive and value == 0):
        raise ValueError("INVALID_" + name)
    return float(value)


def _integer(value, name: str, *, positive=False) -> int:
    _number(value, name, positive=positive)
    if int(value) != value:
        raise ValueError("INVALID_" + name)
    return int(value)


def materials(draft: dict) -> dict:
    return {str(item["id"]): item for rows in draft.get("materials", {}).values()
            if isinstance(rows, list) for item in rows if isinstance(item, dict) and item.get("id")}


def resolve_media(raw: str, draft_root: Path) -> Path:
    path = Path(str(raw))
    if path.is_file():
        return path.resolve()
    fallback = draft_root / "media" / str(raw).replace("\\", "/").rsplit("/", 1)[-1]
    if fallback.is_file():
        return fallback.resolve()
    raise ValueError("MEDIA_MISSING:" + str(raw))


def video_rows(draft: dict, draft_root: Path, overrides: dict | None = None) -> list[dict]:
    """Flatten one baseline video track, preserving its absolute time slots."""
    index = materials(draft)
    segments = sorted((segment for track in draft.get("tracks", []) if track.get("type") == "video"
                       for segment in track.get("segments", [])),
                      key=lambda segment: segment["target_timerange"].get("start", 0))
    replacement = {}
    for row in (overrides or {}).get("overrides", []):
        key = _integer(row.get("index"), "OVERRIDE_INDEX", positive=True)
        if key in replacement or key > len(segments) or not str(row.get("reason") or "").strip():
            raise ValueError("OVERRIDE_INDEX_OR_REASON_INVALID")
        replacement[key] = row
    result, cursor = [], 0
    for number, segment in enumerate(segments, 1):
        target, source = segment["target_timerange"], segment.get("source_timerange") or {}
        start = _integer(target.get("start", 0), "TARGET_START")
        duration = _integer(target["duration"], "TARGET_DURATION", positive=True)
        if start != cursor:
            raise ValueError("VIDEO_TIMELINE_GAP_OR_OVERLAP")
        material = index[str(segment["material_id"])]
        original_source = resolve_media(material.get("path") or material.get("media_path") or "", draft_root)
        original_speed = _number(segment.get("speed", 1), "SPEED", positive=True)
        source_start = _integer(source.get("start", 0), "SOURCE_START")
        source_duration = _integer(source.get("duration", round(duration * original_speed)),
                                   "SOURCE_DURATION", positive=True)
        parts = [{"source": str(original_source), "source_start_us": source_start,
                  "source_end_us": source_start + source_duration, "segment_us": duration,
                  "speed": original_speed}]
        if number in replacement:
            if float(segment.get("volume", 0)) != 0:
                raise ValueError("OVERRIDE_NATIVE_AUDIO_NOT_SUPPORTED")
            parts = replacement[number].get("parts")
            if not isinstance(parts, list) or not parts:
                raise ValueError("OVERRIDE_PARTS_REQUIRED")
            total = sum(_integer(part.get("segment_us"), "PART_DURATION", positive=True) for part in parts)
            if total != duration:
                raise ValueError("OVERRIDE_SENTENCE_DURATION_CHANGED")
        part_cursor = start
        for n, part in enumerate(parts, 1):
            a = _integer(part.get("source_start_us"), "SOURCE_START")
            b = _integer(part.get("source_end_us"), "SOURCE_END", positive=True)
            length = _integer(part.get("segment_us"), "PART_DURATION", positive=True)
            speed = _number(part.get("speed"), "SPEED", positive=True)
            if b <= a or abs((b - a) / speed - length) > 40_000:
                raise ValueError("SOURCE_SPEED_DURATION_MISMATCH")
            source_path = resolve_media(part.get("source") or str(original_source), draft_root)
            result.append({"index": number, "part": n, "source": str(source_path),
                "source_start_us": a, "source_end_us": b, "segment_us": length, "speed": speed,
                "target_start_us": part_cursor, "target_end_us": part_cursor + length,
                "segment": copy.deepcopy(segment), "material": copy.deepcopy(material),
                "changed": number in replacement, "reason": replacement.get(number, {}).get("reason", "")})
            part_cursor += length
        cursor = start + duration
    if not result:
        raise ValueError("VIDEO_SEGMENTS_MISSING")
    if abs(cursor - int(draft.get("duration", cursor))) > 40_000:
        raise ValueError("VIDEO_TIMELINE_DURATION_MISMATCH")
    return result


def ass_stamp(microseconds: int) -> str:
    centi = round(microseconds / 10_000)
    return f"{centi // 360000}:{centi // 6000 % 60:02d}:{centi // 100 % 60:02d}.{centi % 100:02d}"


def subtitle_text(draft: dict, width: int, height: int) -> str:
    index = materials(draft)
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "", "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Microsoft YaHei,29,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,0,5,0,0,0,1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for track in draft.get("tracks", []):
        if track.get("type") != "text":
            continue
        for segment in track.get("segments", []):
            material = index[str(segment["material_id"])]
            content = material.get("content") or {}
            content = json.loads(content) if isinstance(content, str) else content
            body = str(content.get("text") or "")
            body = body.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")
            style = (content.get("styles") or [{}])[0]
            transform = (segment.get("clip") or {}).get("transform") or {}
            x = width * (1 + float(transform.get("x", 0))) / 2
            y = height * (1 - float(transform.get("y", 0))) / 2
            size = float(style.get("size", 8)) * height / 270
            bold = 1 if style.get("bold", True) else 0
            target = segment["target_timerange"]
            tag = "{\\an5\\pos(%.2f,%.2f)\\fs%.2f\\b%d}" % (x, y, size, bold)
            start = target.get("start", 0)
            lines.append(f"Dialogue: 0,{ass_stamp(start)},{ass_stamp(start + target['duration'])},Default,,0,0,0,,{tag}{body}")
    return "\n".join(lines) + "\n"


def atempo(speed: float) -> str:
    values = []
    while speed > 2:
        values.append(2.0)
        speed /= 2
    while speed < .5:
        values.append(.5)
        speed /= .5
    values.append(speed)
    return ",".join(f"atempo={value:.12g}" for value in values)


def _run(command: list[str], cwd: Path, *, timeout=180) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError("FFMPEG_FAILED:" + (result.stderr or result.stdout)[-2500:])
    return result


def _audio_rows(draft: dict, draft_root: Path) -> list[dict]:
    index = materials(draft)
    rows = []
    for track in draft.get("tracks", []):
        if track.get("type") not in {"audio", "video"}:
            continue
        for segment in track.get("segments", []):
            volume = float(segment.get("volume", 1))
            if volume == 0:
                continue
            material = index[str(segment["material_id"])]
            path = resolve_media(material.get("path") or material.get("media_path") or "", draft_root)
            source, target = segment.get("source_timerange") or {}, segment["target_timerange"]
            rows.append({"source": str(path), "source_start_us": source.get("start", 0),
                         "source_duration_us": source.get("duration", target["duration"]),
                         "target_start_us": target.get("start", 0), "target_duration_us": target["duration"],
                         "volume": volume, "track": track.get("name") or track.get("type"),
                         "optional_native_audio": track.get("type") == "video"})
    return rows


def contact_sheets(video: Path, output: Path, *, ffmpeg="ffmpeg", fps=4) -> list[str]:
    """Extract the complete rendered sequence, not only selected hero frames."""
    from PIL import Image, ImageDraw, ImageFont
    frames = output / "frames"
    frames.mkdir()
    _run([ffmpeg, "-hide_banner", "-v", "error", "-i", str(video), "-vf",
          f"fps={fps},scale=180:320", "-q:v", "3", str(frames / "%04d.jpg")], output)
    paths = sorted(frames.glob("*.jpg"))
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    font = ImageFont.truetype(str(font_path), 16) if font_path.is_file() else ImageFont.load_default()
    sheets = []
    for offset in range(0, len(paths), 20):
        sheet = Image.new("RGB", (5 * 180, 4 * 344), "#202020")
        draw = ImageDraw.Draw(sheet)
        for n, path in enumerate(paths[offset:offset + 20]):
            x, y = (n % 5) * 180, (n // 5) * 344
            with Image.open(path) as frame:
                sheet.paste(frame, (x, y + 24))
            draw.text((x + 5, y + 3), f"{(offset+n)/fps:.2f}s", fill="white", font=font)
        target = output / f"sequence_{offset // 20 + 1:02d}.jpg"
        sheet.save(target, quality=90)
        sheets.append(str(target))
    return sheets


def render(draft_path: Path, output: Path, *, overrides: dict | None = None,
           draft_root: Path | None = None, name="review", width=540, height=960,
           fps=30, ffmpeg="ffmpeg", ffprobe="ffprobe", sheets=True) -> dict:
    draft_path = Path(draft_path).resolve()
    draft_root = Path(draft_root).resolve() if draft_root else draft_path.parent
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(draft_root):
        raise ValueError("OUTPUT_MUST_BE_NEW_AND_OUTSIDE_DRAFT")
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError("INVALID_PREVIEW_NAME")
    before = draft_path.read_bytes()
    draft = read(draft_path)
    rows = video_rows(draft, draft_root, overrides)
    audio = _audio_rows(draft, draft_root)
    # A sub-frame protected result cannot be represented faithfully. Reject it
    # before rendering rather than silently dropping or extending that action.
    if any(row["segment_us"] * fps < US for row in rows):
        raise ValueError("PART_SHORTER_THAN_OUTPUT_FRAME")
    output.mkdir(parents=True)
    (output / "subtitles.ass").write_text(subtitle_text(draft, width, height), encoding="utf-8-sig")
    concat, row_receipts = [], []
    for n, row in enumerate(rows, 1):
        frames = round(row["target_end_us"] * fps / US) - round(row["target_start_us"] * fps / US)
        if frames <= 0:
            raise ValueError("PART_SHORTER_THAN_OUTPUT_FRAME")
        target = output / f"part_{n:03d}.mp4"
        filters = [f"setpts=(PTS-STARTPTS)/{row['speed']:.12g}",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease",
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2", "setsar=1", f"fps={fps}",
            "format=yuv420p"]
        for curve in row["segment"].get("common_keyframes") or []:
            if curve.get("property_type") == "KFTypeBrightness" and curve.get("keyframe_list"):
                value = curve["keyframe_list"][0].get("values", [0])[0]
                filters.append(f"eq=brightness={float(value):.6f}")
        filters.append(f"tpad=stop_mode=clone:stop_duration={2/fps:.9f}")
        _run([ffmpeg, "-hide_banner", "-v", "error", "-ss", f"{row['source_start_us']/US:.6f}",
            "-t", f"{(row['source_end_us']-row['source_start_us'])/US:.6f}", "-i", row["source"],
            "-an", "-vf", ",".join(filters), "-frames:v", str(frames), "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "23", "-threads", "2", "-y", str(target)], output)
        concat.append(f"file '{target.name}'")
        row_receipts.append({k: v for k, v in row.items() if k not in {"segment", "material"}} | {"frames": frames})
    (output / "concat.txt").write_text("\n".join(concat) + "\n", encoding="utf-8")
    _run([ffmpeg, "-hide_banner", "-v", "error", "-f", "concat", "-safe", "0", "-i", "concat.txt",
          "-c", "copy", "-y", "picture.mp4"], output)
    inputs, filters, labels, kept_audio = ["-i", "picture.mp4"], ["[0:v]subtitles=subtitles.ass[v]"], [], []
    for row in audio:
        if row["optional_native_audio"]:
            probe = _run([ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "json", row["source"]], output)
            if not json.loads(probe.stdout).get("streams"):
                continue
        inputs += ["-i", row["source"]]
        i = len(kept_audio) + 1
        rate = row["source_duration_us"] / row["target_duration_us"]
        filters.append(f"[{i}:a]atrim=start={row['source_start_us']/US:.6f}:duration={row['source_duration_us']/US:.6f},"
            f"asetpts=PTS-STARTPTS,{atempo(rate)},volume={row['volume']:.9f},"
            f"apad,atrim=duration={row['target_duration_us']/US:.6f},adelay={round(row['target_start_us']/1000)}:all=1[a{i}]")
        labels.append(f"[a{i}]")
        kept_audio.append(row)
    duration = rows[-1]["target_end_us"] / US
    if labels:
        filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,atrim=duration={duration:.6f}[a]")
    else:
        filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f}[a]")
    (output / "filters.txt").write_text(";\n".join(filters), encoding="utf-8")
    target = output / (name + ".mp4")
    _run([ffmpeg, "-hide_banner", "-v", "error", *inputs, "-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", "[a]", "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", "-threads", "2", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-y", str(target)], output)
    _run([ffmpeg, "-hide_banner", "-v", "error", "-xerror", "-i", str(target), "-f", "null", "-"], output)
    probe = json.loads(_run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(target)], output).stdout)
    if draft_path.read_bytes() != before:
        raise ValueError("BASELINE_CHANGED_DURING_PREVIEW")
    receipt = {"status": "DIAGNOSTIC_PROXY_RENDERED", "draft": str(draft_path), "media_root": str(draft_root),
        "output": str(target), "native_export": False, "full_decode": True, "source_unchanged": True,
        "target_duration_s": duration, "actual_duration_s": float(probe["format"]["duration"]),
        "video_parts": row_receipts, "audio_segments": kept_audio,
        "contact_sheets": contact_sheets(target, output, ffmpeg=ffmpeg) if sheets else [],
        "limitations": ["FFmpeg diagnostic proxy, not Jianying native rendering",
                        "Subtitle font metrics approximate; original text, timing and position retained",
                        "Basic brightness is retained; other effects, masks, animated crops and transitions are not reproduced",
                        "Video cut positions quantized to nearest output frame; audio is not retimed by video overrides"]}
    (output / "render_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--draft-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--name", default="review")
    parser.add_argument("--no-contact-sheets", action="store_true")
    args = parser.parse_args(argv)
    receipt = render(args.draft, args.output, overrides=read(args.overrides) if args.overrides else None,
                     draft_root=args.draft_root, name=args.name, sheets=not args.no_contact_sheets)
    print(json.dumps({key: receipt[key] for key in ("status", "output", "full_decode", "actual_duration_s")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
