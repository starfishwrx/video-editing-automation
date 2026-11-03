"""Map a verified edit timeline to an explicit Jianying Headless review plan."""
import json
from pathlib import Path

from ..domain.timeline import TimelineError, VideoLayout, SubtitleStyle
from ..media.timeline_render import timeline_identity
from ..quality.production_qc import check_production


def headless_plan(timeline, name, font_path, *, review_only=False):
    timeline.validate()
    if not review_only:
        raise TimelineError("FORMAL_EXPORT_NOT_IMPLEMENTED")
    if (not name or name != name.strip().strip(".") or ".." in name
            or " ".join(name.split()) != name or any(ord(c) < 32 for c in name)
            or any(c in name for c in '/\\:<>"|?*')):
        raise TimelineError("EXPORT_SINGLE_DRAFT_NAME_REQUIRED")
    if (timeline.canvas.width, timeline.canvas.height,
            timeline.canvas.fps_num, timeline.canvas.fps_den) != (1080, 1920, 30, 1):
        raise TimelineError("EXPORT_CANVAS_UNSUPPORTED")
    font = Path(font_path)
    if not font.is_absolute() or not font.is_file() or font.suffix.lower() not in {".ttf", ".otf"}:
        raise TimelineError("EXPORT_STATIC_LOCAL_FONT_REQUIRED")
    assets = timeline.asset_map()
    videos = []
    for e in timeline.videos:
        a = assets[e.asset_id]
        if (e.layout != VideoLayout() or a.width * 1920 != a.height * 1080
                or not 1 <= e.speed_num / e.speed_den <= 8):
            raise TimelineError("EXPORT_LAYOUT_OR_SPEED_UNSUPPORTED")
        if round(e.duration_us * e.speed_num / e.speed_den) != e.source_duration_us:
            raise TimelineError("EXPORT_SOURCE_ROUNDING_UNSUPPORTED")
        videos.append({"source": a.path, "start_us": e.target_start_us, "duration_us": e.duration_us,
            "source_start_us": e.source_start_us, "source_duration_us": e.source_duration_us,
            "speed": e.speed_num / e.speed_den, "volume": 0})
    tracks = [{"type": "video", "name": "Video_BRoll", "segments": videos}]
    for role, title in (("narration", "Narration"), ("bgm", "BGM"), ("original", "Original")):
        events = sorted((e for e in timeline.audios if e.role == role), key=lambda e: e.target_start_us)
        if any(a.target_start_us + a.duration_us > b.target_start_us for a, b in zip(events, events[1:])):
            raise TimelineError("EXPORT_OVERLAPPING_AUDIO_ROLE_UNSUPPORTED")
        if any(assets[e.asset_id].kind != "audio" or e.fade_in_us or e.fade_out_us or e.gain > 4 for e in events):
            raise TimelineError("EXPORT_AUDIO_CAPABILITY_UNSUPPORTED")
        if events:
            tracks.append({"type": "audio", "name": title, "segments": [
                {"source": assets[e.asset_id].path, "start_us": e.target_start_us, "duration_us": e.duration_us,
                 "source_start_us": e.source_start_us, "source_duration_us": e.duration_us,
                 "speed": 1, "volume": e.gain} for e in events]})
    cues = []
    for cue in sorted(timeline.subtitles, key=lambda e: e.start_us):
        if cue.style != SubtitleStyle():
            raise TimelineError("EXPORT_SUBTITLE_STYLE_UNSUPPORTED")
        cues.append({"text": cue.text, "start_us": cue.start_us, "duration_us": cue.duration_us,
            "size": 8, "bold": cue.style.bold, "x": cue.style.x, "y": cue.style.y, "color": cue.style.color,
            "border_color": "#000000", "border_width": .05, "font_path": str(font)})
    if cues:
        tracks.append({"type": "text", "name": "Subtitles", "segments": cues})
    return {"schema": "jy14-headless-plan/v1", "name": name,
            "canvas": {"width": 1080, "height": 1920, "fps": 30}, "tracks": tracks}


def prepare_headless_export(timeline, *, render_dir, name, font_path, report_dir,
                            review_only=False, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    plan = headless_plan(timeline, name, font_path, review_only=review_only)
    root = Path(report_dir).resolve()
    if root.exists():
        raise TimelineError("EXPORT_DESTINATION_EXISTS")
    check = check_production(timeline, render_dir, ffmpeg=ffmpeg, ffprobe=ffprobe)
    if check["structure_media"]["status"] != "PASS":
        raise TimelineError("EXPORT_RENDER_EVIDENCE_INVALID")
    root.mkdir(parents=True, exist_ok=False)
    (root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    receipt = {"schema": "headless-review-plan-v1", "status": "HEADLESS_REVIEW_PLAN_READY",
        "task_id": timeline.task_id, "revision": timeline.revision,
        "timeline_identity": timeline_identity(timeline), "plan": str(root / "plan.json"),
        "delivery_mode": "preview", "formal_delivery_allowed": False,
        "content_issues": list(timeline.unresolved), "native_ui_acceptance": "pending",
        "limitations": ["Native font, size and frame sampling are not visually certified",
                        "Selected local static font replaces the libass proxy font"]}
    (root / "export_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt
