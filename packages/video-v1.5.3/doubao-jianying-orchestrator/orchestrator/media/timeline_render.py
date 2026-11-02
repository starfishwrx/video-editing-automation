"""Execute the shared timeline without creating or reading a Jianying draft."""
from bisect import bisect_left, bisect_right
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import wave

from ..domain.timeline import EditTimeline, TimelineError, US
from . import action_frames
from .probe import probe_media
from .video_decode import run_ffmpeg_decode
from .video_encode import run_h264_encode

SAMPLE_RATE = 48000


def timeline_identity(timeline):
    return hashlib.sha256(json.dumps(timeline.to_dict(), sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _run(command, *, timeout=180):
    return subprocess.run([str(x) for x in command], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=True)


def _save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _no_sync_changes(proc):
    if any(int(n) for n in re.findall(r"\b(?:dup|drop)=\s*(\d+)", proc.stderr or "")):
        raise TimelineError("ENCODER_DUPLICATED_OR_DROPPED_SOURCE_FRAME")


def _samples(time_us):
    return (time_us * SAMPLE_RATE + US // 2) // US


def _wav_samples(path):
    with wave.open(str(path), "rb") as audio:
        if audio.getframerate() != SAMPLE_RATE or audio.getnchannels() != 2:
            raise TimelineError("RENDER_AUDIO_FORMAT_MISMATCH")
        return audio.getnframes()


def _stamp(us):
    # One quantizer for both sides of adjacent cues; outward rounding overlaps them.
    centiseconds = us // 10000
    return f"{centiseconds // 360000}:{centiseconds // 6000 % 60:02}:{centiseconds // 100 % 60:02}.{centiseconds % 100:02}"


def subtitle_ass(timeline, width, height):
    header = ("[Script Info]\nScriptType: v4.00+\n" + f"PlayResX: {width}\nPlayResY: {height}\n"
              "WrapStyle: 2\n[V4+ Styles]\n"
              "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
              "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
              "Alignment,MarginL,MarginR,MarginV,Encoding\n")
    styles, dialogues = [], []
    for i, cue in enumerate(timeline.subtitles):
        if any(c in cue.text for c in ("\\", "{", "}")):
            raise TimelineError("SUBTITLE_ASS_LITERAL_ESCAPE_UNSUPPORTED")
        if _stamp(cue.start_us) == _stamp(cue.start_us + cue.duration_us):
            raise TimelineError("SUBTITLE_BELOW_ASS_TIME_QUANTUM")
        style = cue.style
        color = "&H00" + style.color[5:7] + style.color[3:5] + style.color[1:3]
        font_size = style.font_size_px * width / timeline.canvas.width
        styles.append(f"Style: s{i},{style.font_name},{font_size:.4f},{color},{color},&H00000000,&H80000000,"
                      f"{-1 if style.bold else 0},0,0,0,100,100,0,0,1,1,0,5,0,0,0,1")
        text = cue.text.replace("\r", "").replace("\n", "\\N")
        x, y = width * (1 + style.x) / 2, height * (1 - style.y) / 2
        dialogues.append(f"Dialogue: 0,{_stamp(cue.start_us)},{_stamp(cue.start_us + cue.duration_us)},"
                         f"s{i},,0,0,0,,{{\\pos({x:.4f},{y:.4f})}}{text}")
    return (header + "\n".join(styles) + "\n[Events]\n"
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n" + "\n".join(dialogues) + "\n")


def _layout(event, source, width, height):
    sw, sh, layout = source["width"], source["height"], event.layout
    factor = (max(width / sw, height / sh) if layout.mode == "cover" else
              min(width / sw, height / sh)) * layout.scale
    w, h = math.ceil(sw * factor / 2) * 2, math.ceil(sh * factor / 2) * 2
    pw, ph = max(w, width) + 2 * width, max(h, height) + 2 * height
    x, y = (pw - width) / 2 - layout.offset_x * width / 2, (ph - height) / 2 + layout.offset_y * height / 2
    return f"scale={w}:{h},pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:black,crop={width}:{height}:{x}:{y},setsar=1"


def _frame_plan(timeline, event, metadata, *, allow_real_tail=False):
    stream, frames = metadata["stream"], metadata["frames"]
    fps = Fraction(stream["avg_frame_rate"])
    # Matroska commonly quantizes 60fps to a 1ms time base: 16/17ms is still CFR.
    quantum = Fraction(stream.get("time_base", "1/1000000")) * US
    tolerance = max(Fraction(2), quantum / 2 + 1)
    if fps <= 0 or any(abs(row["pts_us"] - round(Fraction(i * US, 1) / fps)) > tolerance
                       for i, row in enumerate(frames)):
        raise TimelineError("SOURCE_VFR_UNSUPPORTED:" + event.event_id)
    start, end = timeline.video_frame_range(event)
    source_end = event.source_start_us + event.source_duration_us
    if source_end > metadata["source_end_us"]:
        raise TimelineError("ACTUAL_SOURCE_WINDOW_TOO_SHORT")
    pts = [f["pts_us"] for f in frames]
    first_index = bisect_left(pts, event.source_start_us)
    if first_index >= len(pts):
        raise TimelineError("SOURCE_WINDOW_HAS_NO_FRAME")
    first_pts = pts[first_index]
    result = []
    for relative, output_frame in enumerate(range(start, end)):
        source_time = first_pts + Fraction(
            relative * US * timeline.canvas.fps_den * event.speed_num,
            timeline.canvas.fps_num * event.speed_den)
        index = bisect_right(pts, source_time + quantum / 2) - 1
        preview_tail = (allow_real_tail and timeline.delivery_mode == "preview"
            and event.target_start_us + event.duration_us == timeline.duration_us and relative > 0
            and output_frame == end - 1
            and 0 <= index < len(pts) and source_end <= pts[index] < source_end +
                math.ceil(Fraction(US * timeline.canvas.fps_den * event.speed_num,
                    timeline.canvas.fps_num * event.speed_den) + US / fps))
        if index < 0 or index >= len(pts) or not (event.source_start_us <= pts[index] < source_end or preview_tail):
            raise TimelineError("OUTPUT_FRAME_OUTSIDE_SOURCE_WINDOW")
        result.append({"output_frame": output_frame, "source_frame": index, "source_pts_us": pts[index]})
        if preview_tail:
            result[-1]["preview_real_tail_us"] = pts[index] - source_end
    if any(a["source_frame"] >= b["source_frame"] for a, b in zip(result, result[1:])):
        raise TimelineError("REPEATED_SOURCE_FRAME_UNSUPPORTED")
    return result


def _protected_counts(event, mapping):
    result = []
    for interval in event.protected_intervals:
        count = sum(interval.source_start_us <= f["source_pts_us"] < interval.source_end_us for f in mapping)
        if count < interval.min_output_frames:
            raise TimelineError("PROTECTED_OUTPUT_FRAMES_MISSING:" + interval.interval_id)
        result.append({"interval_id": interval.interval_id, "actual_output_frames": count,
                       "required_frames": interval.min_output_frames})
    return result


def _selection_expression(mapping):
    # A flat a+b+c+... exceeds FFmpeg's expression recursion limit on real
    # multi-sentence actions. Balance the same exact membership set, not its FPS.
    parts = [f"eq(n\\,{row['source_frame']})" for row in mapping]
    if not parts:
        raise TimelineError("RENDER_SELECTION_EMPTY")
    while len(parts) > 1:
        parts = ["(" + parts[i] + "+" + parts[i + 1] + ")" if i + 1 < len(parts) else parts[i]
                 for i in range(0, len(parts), 2)]
    return parts[0]


def verify_video(path, *, expected_frames, fps, ffmpeg, ffprobe):
    proc = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames,avg_frame_rate", "-of", "json", path])
    stream = json.loads(proc.stdout)["streams"][0]
    if int(stream.get("nb_read_frames", -1)) != expected_frames or Fraction(stream["avg_frame_rate"]) != fps:
        raise TimelineError("RENDER_OUTPUT_FRAME_COUNT_OR_RATE_MISMATCH")
    run_ffmpeg_decode([str(ffmpeg), "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=True)
    return {"frames": expected_frames, "fps": str(fps), "full_decode": True}


def _render_audio(timeline, directory, *, duration_us, ffmpeg, facts):
    total_samples = _samples(duration_us)
    by_role = {}
    for i, event in enumerate(timeline.audios):
        a = facts[event.asset_id]
        if not a["audio"] or event.source_start_us + event.duration_us > a["duration_us"]:
            raise TimelineError("ACTUAL_AUDIO_WINDOW_TOO_SHORT_OR_STREAM_MISSING")
        if event.role == "original" and timeline.asset_map()[event.asset_id].kind == "video":
            video_stream = next(s for s in a["streams"] if s.get("codec_type") == "video")
            audio_stream = next(s for s in a["streams"] if s.get("codec_type") == "audio")
            if abs(Fraction(video_stream.get("start_time") or "0") -
                   Fraction(audio_stream.get("start_time") or "0")) > Fraction(1, US):
                raise TimelineError("ORIGINAL_AUDIO_STREAM_OFFSET_UNSUPPORTED")
        path = directory / f"audio-event-{i:04}.wav"
        first, count = _samples(event.source_start_us), _samples(event.duration_us)
        af = (f"aresample={SAMPLE_RATE},atrim=start_sample={first}:end_sample={first + count},"
              f"asetpts=PTS-STARTPTS,volume={event.gain}")
        if event.fade_in_us:
            af += f",afade=t=in:st=0:d={event.fade_in_us / US:.9f}"
        if event.fade_out_us:
            af += f",afade=t=out:st={(event.duration_us - event.fade_out_us) / US:.9f}:d={event.fade_out_us / US:.9f}"
        _run([ffmpeg, "-v", "error", "-nostdin", "-i", a["path"], "-vn", "-af", af,
              "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le", path])
        if _wav_samples(path) != count:
            raise TimelineError("DECODED_AUDIO_SAMPLE_COUNT_MISMATCH")
        by_role.setdefault(event.role, []).append((event, path))
    tracks = {}
    def mix(inputs, output, delays):
        cmd, filters = [ffmpeg, "-v", "error", "-nostdin"], []
        for i, path in enumerate(inputs):
            cmd += ["-i", path]
            filters.append(f"[{i}:a]adelay={delays[i]}S:all=1[a{i}]")
        filters.append("".join(f"[a{i}]" for i in range(len(inputs))) +
                       f"amix=inputs={len(inputs)}:normalize=0:duration=longest,"
                       f"apad=whole_len={total_samples},atrim=end_sample={total_samples}[out]")
        _run(cmd + ["-filter_complex", ";".join(filters), "-map", "[out]", "-ar", str(SAMPLE_RATE),
                    "-ac", "2", "-c:a", "pcm_s16le", output])
        if _wav_samples(output) != total_samples:
            raise TimelineError("MIXED_AUDIO_SAMPLE_COUNT_MISMATCH")
    for role, items in by_role.items():
        track = directory / (role + ".wav")
        mix([p for _, p in items], track, [_samples(e.target_start_us) for e, _ in items])
        tracks[role] = str(track)
    output = directory / "mix.wav"
    if tracks:
        mix(list(tracks.values()), output, [0] * len(tracks))
    else:
        _run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
              "-af", f"atrim=end_sample={total_samples}", "-c:a", "pcm_s16le", output])
    return {"path": str(output), "tracks": tracks, "samples": total_samples, "sample_rate": SAMPLE_RATE}


def render_timeline(timeline: EditTimeline, output_dir, *, ffmpeg="ffmpeg", ffprobe="ffprobe", width=None,
                    allow_real_tail=False):
    """Write only into a new directory; the success receipt is always written last."""
    timeline.validate()
    if allow_real_tail and timeline.delivery_mode != "preview":
        raise TimelineError("REAL_TAIL_REQUIRES_DIAGNOSTIC_PREVIEW")
    root = Path(output_dir).resolve()
    if root.exists():
        raise TimelineError("RENDER_OUTPUT_EXISTS")
    width = timeline.canvas.width if width is None else width
    if type(width) is not int or width < 32 or width % 2 or width > timeline.canvas.width:
        raise TimelineError("RENDER_WIDTH_INVALID")
    height = round(timeline.canvas.height * width / timeline.canvas.width / 2) * 2
    facts = {a.asset_id: probe_media(a.path, ffprobe=ffprobe) for a in timeline.assets}
    before = {a.asset_id: action_frames.identity(a.path) for a in timeline.assets}
    for asset in timeline.assets:
        actual = facts[asset.asset_id]
        if asset.kind == "video" and (actual.get("width") != asset.width or actual.get("height") != asset.height
                or (actual.get("fps_num"), actual.get("fps_den")) != (asset.fps_num, asset.fps_den)):
            raise TimelineError("ACTUAL_VIDEO_METADATA_MISMATCH")
        if abs(actual["duration_us"] - asset.duration_us) > 1:
            raise TimelineError("ACTUAL_ASSET_DURATION_CHANGED")
        for stream in actual["streams"]:
            if stream.get("codec_type") == "video":
                if stream.get("sample_aspect_ratio", "1:1") not in ("1:1", "N/A", "0:1"):
                    raise TimelineError("SOURCE_NON_SQUARE_PIXELS_UNSUPPORTED")
                if any(s.get("rotation", 0) for s in stream.get("side_data_list", [])):
                    raise TimelineError("SOURCE_ROTATION_UNSUPPORTED")
    root.mkdir(parents=True)
    timeline.save(root / "timeline.json")
    fps = Fraction(timeline.canvas.fps_num, timeline.canvas.fps_den)
    mappings, protected, clips = [], [], []
    try:
        for i, event in enumerate(timeline.videos):
            source = facts[event.asset_id]
            metadata = action_frames.probe(source["path"], root / "source_pts", ffprobe=str(ffprobe))
            mapping = _frame_plan(timeline, event, metadata, allow_real_tail=allow_real_tail)
            # The decoder's observed PTS, before any setpts, must match the source plan.
            expression = _selection_expression(mapping)
            if len(expression) > 24000:
                raise TimelineError("RENDER_EVENT_FRAME_BUDGET_EXCEEDED")
            vf = (f"select='{expression}',showinfo," + _layout(event, source, width, height) +
                  f",settb=1/{timeline.canvas.fps_num},setpts=N*{timeline.canvas.fps_den}")
            clip = root / f"video-{i:04}.mp4"
            _, proc = run_h264_encode(str(ffmpeg), ["-hide_banner", "-nostdin", "-v", "info", "-copyts",
                "-threads", "1", "-i", source["path"], "-an", "-vf", vf,
                "-fps_mode", "cfr", "-r", str(fps), "-frames:v", str(len(mapping))], clip,
                trailing=["-pix_fmt", "yuv420p"], timeout=180)
            _no_sync_changes(proc)
            units = re.findall(r"config in time_base:\s*(\d+/\d+)", proc.stderr or "")
            observed = re.findall(r"\bn:\s*\d+\s+pts:\s*(-?\d+)", proc.stderr or "")
            if not units or len(observed) != len(mapping):
                raise TimelineError("RENDER_SOURCE_DECODE_INCOMPLETE")
            unit = Fraction(units[-1])
            for row, raw_pts in zip(mapping, observed):
                pts = round(int(raw_pts) * unit * US) - metadata["timestamp_origin_us"]
                if abs(pts - row["source_pts_us"]) > 1:
                    raise TimelineError("RENDER_SOURCE_PTS_MISMATCH")
                row["decoded_source_pts_us"] = pts
            protected.extend({"event_id": event.event_id, **p} for p in _protected_counts(event, mapping))
            verify_video(clip, expected_frames=len(mapping), fps=fps, ffmpeg=ffmpeg, ffprobe=ffprobe)
            mappings.append({"event_id": event.event_id, "asset_id": event.asset_id, "frames": mapping})
            clips.append(clip)
        render_us = round(timeline.frame_count * US / fps)
        audio = _render_audio(timeline, root, duration_us=render_us, ffmpeg=ffmpeg, facts=facts)
        listing = root / "clips.txt"
        listing.write_text("\n".join(f"file '{clip.name}'" for clip in clips), encoding="utf-8")
        ass = root / "subtitles.ass"
        ass.write_text(subtitle_ass(timeline, width, height), encoding="utf-8-sig")
        output = root / "preview.mp4"
        leading = ["-v", "info", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(listing),
                   "-i", audio["path"], "-map", "0:v:0", "-map", "1:a:0"]
        if timeline.subtitles:
            escaped = str(ass).replace("\\", "/").replace(":", "\\:").replace("'", "'\\\\\\''")
            leading += ["-vf", "ass=filename='" + escaped + "'"]
        leading += ["-fps_mode", "cfr", "-r", str(fps), "-frames:v", str(timeline.frame_count)]
        _, final_proc = run_h264_encode(str(ffmpeg), leading, output,
                        trailing=["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"], timeout=300)
        _no_sync_changes(final_proc)
        verified = verify_video(output, expected_frames=timeline.frame_count, fps=fps,
                                 ffmpeg=ffmpeg, ffprobe=ffprobe)
        if not probe_media(output, ffprobe=ffprobe)["audio"]:
            raise TimelineError("RENDER_OUTPUT_AUDIO_MISSING")
        for asset in timeline.assets:
            if action_frames.identity(asset.path) != before[asset.asset_id]:
                raise TimelineError("SOURCE_CHANGED_DURING_RENDER")
        receipt = {"schema": "timeline-render-v1", "status": "rendered", "content_status": "UNCERTIFIED",
            "formal_delivery_allowed": False, "task_id": timeline.task_id, "revision": timeline.revision,
            "timeline_identity": timeline_identity(timeline), "assets": before, "video": str(output),
            "output_identity": action_frames.identity(output),
            "width": width, "height": height, "duration_us": timeline.duration_us,
            "render_duration_us": render_us, "quantization_overhang_us": render_us - timeline.duration_us,
            "video_verification": verified, "audio": audio,
            "audio_identities": {p: action_frames.identity(p) for p in
                                  [audio["path"], *audio["tracks"].values()]},
            "frame_mapping": mappings, "protected": protected,
            "real_tail_preview": bool(allow_real_tail),
            "real_tail_frames": [dict(event_id=group["event_id"], **row) for group in mappings
                for row in group["frames"] if "preview_real_tail_us" in row],
            "subtitle_rendering": {"path": str(ass), "method": "libass", "time_quantum_us": 10000,
                "font_selection": [line.strip() for line in (final_proc.stderr or "").splitlines() if "fontselect:" in line],
                "fidelity": "basic layout proxy; font metrics and Jianying editing not verified"},
            "unresolved": list(timeline.unresolved)}
        _save(root / "render_receipt.json", receipt)
        return receipt
    except Exception as exc:
        _save(root / "render_failure.json", {"status": "BLOCKED", "reason": str(exc),
                                             "formal_delivery_allowed": False})
        raise
