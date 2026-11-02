"""One-way adapter from fixed selections to a writer-independent editing result.

This first production slice consumes prepared audio and selected windows. It does
not select shots, synthesize speech, write drafts or certify visual content.
"""
from collections import OrderedDict
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import unicodedata

from ..audio.final_assets import prepare_narration
from ..domain.timeline import (Asset, AudioEvent, Canvas, EditTimeline, Sentence,
                               SourceInterval, SubtitleEvent, SubtitleStyle,
                               TimelineError, VideoEvent, VideoLayout)
from ..media.probe import probe_media
from ..policy.production_parameters import subtitle_transform
from ..runtime.production_env import production_environment


def _us(value):
    return round(Fraction(str(value)) * 1_000_000)


def _resolve(value, base):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise TimelineError("MEDIA_PATH_REQUIRED")
    path = Path(value)
    return (path if path.is_absolute() else Path(base) / path).resolve()


def _display_text(value):
    return "".join(c for c in value if not unicodedata.category(c).startswith("P"))


def _selected_rows(manifest, clips):
    if clips is not None:
        return [dict(vars(c)) if not isinstance(c, dict) else dict(c) for c in clips]
    result = []
    for index, raw in enumerate(manifest.get("segments", [])):
        if not isinstance(raw, dict):
            raise TimelineError("SEGMENT_OBJECT_REQUIRED")
        parts = raw.get("span_parts")
        if parts:
            for part in parts:
                row = {**raw, **part, "raw_index": index}
                row["duration"] = part.get("segment_s", part.get("duration", 0))
                row["duration_us"] = part.get("duration_us", _us(row["duration"]))
                row["video_speed"] = part.get("video_speed", part.get("speed", raw.get("video_speed", 1)))
                # Each child owns its window; a parent's full window is not a child default.
                for unit in ("source_start_us", "source_end_us"):
                    if unit not in part:
                        row.pop(unit, None)
                row["source_start"] = part.get("source_start", 0)
                row["source_end"] = part.get("source_end", 0)
                row["solved_window"] = dict(part) if "ok" in part else None
                row["window_solution"] = None
                row["text"] = raw.get("text", raw.get("caption", ""))
                row["audio"] = raw.get("audio")
                row["tts_path"] = raw.get("tts_path")
                result.append(row)
        else:
            result.append({**raw, "raw_index": index})
    if not result:
        raise TimelineError("FIXED_SELECTION_REQUIRED")
    return result


def prepare_timeline(manifest, base_dir, report_dir, *, clips=None, revision=1,
                     ffmpeg=None, ffprobe=None):
    """Finish audio, then adapt selected Clip/manifest data without finding a draft root."""
    environment = production_environment(ffmpeg=ffmpeg, ffprobe=ffprobe)
    if not environment["ok"]:
        raise TimelineError("PRODUCTION_ENVIRONMENT_BLOCKED:" + ",".join(environment["issues"]))
    ffmpeg, ffprobe = environment["tools"]["ffmpeg"], environment["tools"]["ffprobe"]
    report_dir, base_dir = Path(report_dir), Path(base_dir)
    if report_dir.exists():
        raise TimelineError("PRODUCTION_REPORT_EXISTS_USE_NEW_REVISION_DIRECTORY")
    for key in ("effects", "filters", "transitions", "brightness", "adjustments", "source_frame_map"):
        if manifest.get(key):
            raise TimelineError("PRODUCTION_FEATURE_UNSUPPORTED:" + key)
    rows = _selected_rows(manifest, clips)
    if type(manifest.get("subtitles", True)) is not bool:
        raise TimelineError("SUBTITLE_MODE_UNSUPPORTED")
    for row in rows:
        if any(row.get(k) for k in ("transition", "effects", "source_frame_map", "frame_map",
                                    "brightness", "filters", "adjustments", "crop", "mask",
                                    "video_effects", "subtitle_effects")):
            raise TimelineError("VIDEO_FEATURE_UNSUPPORTED")
    report_dir.mkdir(parents=True, exist_ok=False)
    canvas_spec = manifest.get("canvas", {})
    fps = Fraction(str(manifest.get("fps", 30)))
    aspect = manifest.get("aspect_ratio", "9:16")
    if aspect not in {"9:16", "16:9"}:
        raise TimelineError("ASPECT_RATIO_UNSUPPORTED")
    default_w, default_h = (1080, 1920) if aspect == "9:16" else (1920, 1080)
    canvas = Canvas(**({"width": manifest.get("width", default_w), "height": manifest.get("height", default_h),
                        "fps_num": fps.numerator, "fps_den": fps.denominator} | canvas_spec))
    style_spec = dict(manifest.get("subtitle_style", {}))
    if manifest.get("subtitle_position_px") is not None:
        position = subtitle_transform(manifest, canvas.width, canvas.height)
        if any(k in style_spec and style_spec[k] != v for k, v in position.items()):
            raise TimelineError("SUBTITLE_POSITION_CONFLICT")
        style_spec.update(position)
    assets, asset_paths, probes, videos, audios, sentences, subtitles, audio_details = [], {}, {}, [], [], [], [], []
    def asset(path, kind):
        path = _resolve(path, base_dir)
        key = (str(path), kind)
        if key not in asset_paths:
            facts = probe_media(path, ffprobe=ffprobe)
            if kind == "video" and "width" not in facts:
                raise TimelineError("VIDEO_STREAM_MISSING")
            if kind == "audio" and not facts["audio"]:
                raise TimelineError("AUDIO_STREAM_MISSING")
            aid = "asset-%04d" % (len(assets) + 1)
            assets.append(Asset(aid, str(path), kind, facts["duration_us"],
                facts.get("width", 0), facts.get("height", 0), facts.get("fps_num", 0), facts.get("fps_den", 1)))
            asset_paths[key], probes[aid] = aid, facts
        return asset_paths[key]
    groups = OrderedDict()
    previous, closed = None, set()
    for index, row in enumerate(rows):
        raw_index = row.get("raw_index", -1)
        parent = row.get("parent_index", -1)
        key = raw_index if type(raw_index) is int and raw_index >= 0 else (
              parent if type(parent) is int and parent >= 0 else index)
        if previous is not None and key != previous:
            closed.add(previous)
        if key in closed:
            raise TimelineError("SENTENCE_PARTS_NOT_CONTIGUOUS")
        groups.setdefault(key, []).append(row)
        previous = key
    cursor = 0
    for key, group in groups.items():
        first = group[0]
        sid = first.get("sentence_id") or "sentence-%04d" % (key + 1)
        text = str(first.get("text", first.get("caption", "")) or "").strip()
        audio_path = first.get("tts_path") or first.get("audio")
        mode = first.get("audio_mode", "file" if first.get("audio") else "tts")
        final = None
        for row in group:
            if str(row.get("text", row.get("caption", "")) or "").strip() != text:
                raise TimelineError("SENTENCE_TEXT_CONFLICT")
            if (row.get("tts_path") or row.get("audio")) != audio_path:
                raise TimelineError("SENTENCE_AUDIO_CONFLICT")
            if row.get("visual_missing") or row.get("degraded_no_match") or row.get("degraded"):
                raise TimelineError("SELECTION_UNRESOLVED")
        if text:
            if not audio_path or mode not in {"tts", "file"}:
                raise TimelineError("PREPARED_NARRATION_REQUIRED:" + sid)
            final = prepare_narration(_resolve(audio_path, base_dir), report_dir / "audio", mode=mode,
                loudness=manifest.get("narration_loudness"), ffmpeg=ffmpeg, ffprobe=ffprobe)
            audio_details.append({"sentence_id": sid, **final})
            audio_duration = final["duration_us"]
            narration_id = "narration-" + sid
            origin_id = asset(final["source"], "audio")
            aid = asset(final["path"], "audio")
            if aid != origin_id:
                assets[:] = [replace(a, origin_asset_id=origin_id) if a.asset_id == aid else a for a in assets]
            audios.append(AudioEvent(narration_id, aid, "narration", cursor, audio_duration,
                                    sentence_ids=(sid,)))
            requirements = first.get("visual_requirements") or first.get("required_visuals") or ()
            if isinstance(requirements, (str, dict)):
                requirements = (requirements,)
            sentences.append(Sentence(sid, text, cursor, audio_duration, narration_id, tuple(requirements)))
            if manifest.get("subtitles", True) is not False:
                style = SubtitleStyle(**style_spec)
                subtitles.append(SubtitleEvent("subtitle-" + sid, sid, _display_text(text), cursor,
                                               audio_duration, style))
        if len(group) > 1:
            durations = [r.get("duration_us", _us(r.get("duration", 0))) for r in group]
            if final and sum(durations) != final["duration_us"]:
                raise TimelineError("AUDIO_REPLAN_REQUIRED:" + sid)
        else:
            durations = [final["duration_us"] if final else
                         first.get("duration_us", _us(first.get("duration", 0)))]
            if clips is not None and final and (
                    first.get("duration_us", _us(first.get("duration", 0))) != final["duration_us"]):
                raise TimelineError("AUDIO_REPLAN_REQUIRED:" + sid)
        for row, duration in zip(group, durations):
            if clips is not None and row.get("start_us", cursor) != cursor:
                raise TimelineError("TARGET_POSITION_CONFLICT")
            if final and row.get("audio_duration_us") and len(group) == 1 and (
                    row["audio_duration_us"] != final["duration_us"]):
                raise TimelineError("AUDIO_REPLAN_REQUIRED:" + sid)
            aid = asset(row.get("video"), "video")
            start = row.get("source_start_us", _us(row.get("source_start", row.get("in", 0))))
            speed = Fraction(str(row.get("video_speed", row.get("speed", 1))))
            end = row.get("source_end_us")
            if end is None and row.get("source_end"):
                end = _us(row["source_end"])
            source_duration = end - start if end is not None else round(duration * speed)
            solution = row.get("solved_window") or row.get("window_solution")
            if solution:
                if not isinstance(solution, dict) or solution.get("ok") is not True:
                    raise TimelineError("WINDOW_SOLUTION_UNRESOLVED")
                if ("source_start" in solution and _us(solution["source_start"]) != start) or (
                        "source_end" in solution and _us(solution["source_end"]) != start + source_duration):
                    raise TimelineError("SOURCE_WINDOW_SOLUTION_MISMATCH")
            layout = VideoLayout(**row.get("layout", {}))
            protected = tuple(SourceInterval(**p) for p in row.get("protected_intervals", []))
            evidence = row.get("evidence_intervals") or ()
            if isinstance(evidence, dict):
                evidence = (evidence,)
            videos.append(VideoEvent("video-%04d" % (len(videos) + 1), aid, (sid,) if text else (),
                cursor, duration, start, source_duration, speed.numerator, speed.denominator,
                layout, protected, tuple(evidence)))
            if row.get("keep_original_audio") or mode == "native":
                if speed != 1:
                    raise TimelineError("ORIGINAL_AUDIO_RETIMING_UNSUPPORTED")
                audios.append(AudioEvent("original-" + videos[-1].event_id, aid, "original", cursor,
                    duration, start, row.get("original_gain", 1.0), sentence_ids=(sid,) if text else ()))
            cursor += duration
    bgm = manifest.get("bgm")
    if bgm:
        if not isinstance(bgm, dict) or not bgm.get("path"):
            raise TimelineError("PREPARED_BGM_PATH_REQUIRED")
        if set(bgm) - {"path", "gain", "fade_in", "fade_out", "source_start_us"}:
            raise TimelineError("BGM_OPTION_UNSUPPORTED")
        aid = asset(bgm["path"], "audio")
        audios.append(AudioEvent("bgm", aid, "bgm", 0, cursor, bgm.get("source_start_us", 0),
            bgm.get("gain", 1.0), _us(bgm.get("fade_in", 0)), _us(bgm.get("fade_out", 0))))
    timeline = EditTimeline(str(manifest.get("task_id") or manifest.get("record_id") or "fixed-selection"),
        revision, canvas, cursor, tuple(assets), tuple(sentences), tuple(videos), tuple(audios),
        tuple(subtitles), manifest.get("delivery_mode", "formal"), tuple(manifest.get("unresolved", [])))
    timeline.validate()
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "prepared_assets.json").write_text(json.dumps({"environment": environment,
        "narration": audio_details, "assets": probes}, ensure_ascii=False, indent=2), encoding="utf-8")
    timeline.save(report_dir / "timeline.json")
    return timeline
