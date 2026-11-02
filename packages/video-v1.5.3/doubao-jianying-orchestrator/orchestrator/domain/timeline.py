"""Shared editing result. No writer, probes, service calls or QC verdicts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import math
from pathlib import Path

US = 1_000_000
SUPPORTED_CAPABILITIES = ("hard_cut", "constant_speed", "basic_layout", "audio_gain",
                          "audio_fades", "sentence_subtitles")


class TimelineError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise TimelineError(message)


def _int(value, name, minimum=0):
    _require(type(value) is int and value >= minimum, f"INTEGER_INVALID:{name}")


def _number(value, name, minimum=None):
    _require(type(value) in (int, float) and math.isfinite(value), f"NUMBER_INVALID:{name}")
    if minimum is not None:
        _require(value >= minimum, f"NUMBER_RANGE:{name}")


def _text(value, name):
    _require(isinstance(value, str) and bool(value.strip()), f"TEXT_REQUIRED:{name}")


def frame_at_us(time_us: int, fps_num: int, fps_den: int = 1) -> int:
    for value, name, minimum in ((time_us, "time_us", 0), (fps_num, "fps_num", 1),
                                  (fps_den, "fps_den", 1)):
        _int(value, name, minimum)
    return time_us * fps_num // (US * fps_den)


def frame_time_us(frame: int, fps_num: int, fps_den: int = 1) -> int:
    _int(frame, "frame", 0)
    _int(fps_num, "fps_num", 1)
    _int(fps_den, "fps_den", 1)
    return (frame * US * fps_den + fps_num // 2) // fps_num


@dataclass(frozen=True)
class Canvas:
    width: int = 1080
    height: int = 1920
    fps_num: int = 30
    fps_den: int = 1


@dataclass(frozen=True)
class Asset:
    asset_id: str
    path: str
    kind: str
    duration_us: int
    width: int = 0
    height: int = 0
    fps_num: int = 0
    fps_den: int = 1
    origin_asset_id: str | None = None


@dataclass(frozen=True)
class Sentence:
    sentence_id: str
    text: str
    start_us: int
    duration_us: int
    narration_id: str | None = None
    requirements: tuple = ()


@dataclass(frozen=True)
class VideoLayout:
    mode: str = "cover"
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    opacity: float = 1.0


@dataclass(frozen=True)
class SourceInterval:
    interval_id: str
    source_start_us: int
    source_end_us: int
    kind: str = "action"
    min_output_frames: int = 1


@dataclass(frozen=True)
class VideoEvent:
    event_id: str
    asset_id: str
    sentence_ids: tuple[str, ...]
    target_start_us: int
    duration_us: int
    source_start_us: int
    source_duration_us: int
    speed_num: int = 1
    speed_den: int = 1
    layout: VideoLayout = field(default_factory=VideoLayout)
    protected_intervals: tuple[SourceInterval, ...] = ()
    evidence: tuple = ()


@dataclass(frozen=True)
class AudioEvent:
    event_id: str
    asset_id: str
    role: str
    target_start_us: int
    duration_us: int
    source_start_us: int = 0
    gain: float = 1.0
    fade_in_us: int = 0
    fade_out_us: int = 0
    sentence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubtitleStyle:
    font_name: str = "Microsoft YaHei"
    font_size_px: float = 56.0
    color: str = "#FFFFFF"
    bold: bool = True
    x: float = 0.0
    y: float = -0.55


@dataclass(frozen=True)
class SubtitleEvent:
    event_id: str
    sentence_id: str
    text: str
    start_us: int
    duration_us: int
    style: SubtitleStyle = field(default_factory=SubtitleStyle)


@dataclass(frozen=True)
class EditTimeline:
    task_id: str
    revision: int
    canvas: Canvas
    duration_us: int
    assets: tuple[Asset, ...]
    sentences: tuple[Sentence, ...]
    videos: tuple[VideoEvent, ...]
    audios: tuple[AudioEvent, ...] = ()
    subtitles: tuple[SubtitleEvent, ...] = ()
    delivery_mode: str = "formal"
    unresolved: tuple = ()
    capabilities: tuple[str, ...] = SUPPORTED_CAPABILITIES
    schema: str = "edit-timeline-v1"

    @property
    def frame_count(self):
        # Last picture covers the whole narration. No -shortest or synthetic tail frame.
        n = self.duration_us * self.canvas.fps_num
        d = US * self.canvas.fps_den
        return (n + d - 1) // d

    def video_frame_range(self, event):
        c = self.canvas
        start = frame_at_us(event.target_start_us, c.fps_num, c.fps_den)
        end_us = event.target_start_us + event.duration_us
        end = (self.frame_count if end_us == self.duration_us else
               frame_at_us(end_us, c.fps_num, c.fps_den))
        return start, end

    def asset_map(self):
        return {a.asset_id: a for a in self.assets}

    def sentence_map(self):
        return {s.sentence_id: s for s in self.sentences}

    def validate(self):
        _require(self.schema == "edit-timeline-v1", "TIMELINE_SCHEMA_UNSUPPORTED")
        _text(self.task_id, "task_id")
        _int(self.revision, "revision", 1)
        _int(self.duration_us, "duration_us", 1)
        _require(self.delivery_mode in {"formal", "preview"}, "DELIVERY_MODE_INVALID")
        _require(isinstance(self.canvas, Canvas), "CANVAS_INVALID")
        c = self.canvas
        for value, name in ((c.width, "width"), (c.height, "height"),
                            (c.fps_num, "fps_num"), (c.fps_den, "fps_den")):
            _int(value, name, 1)
        _require(c.width % 2 == c.height % 2 == 0, "CANVAS_REQUIRES_EVEN_SIZE")
        _require(set(self.capabilities).issubset(SUPPORTED_CAPABILITIES), "CAPABILITY_UNSUPPORTED")
        for name in ("assets", "sentences", "videos", "audios", "subtitles", "unresolved", "capabilities"):
            _require(isinstance(getattr(self, name), (list, tuple)), f"SEQUENCE_REQUIRED:{name}")
        _require(bool(self.videos), "VIDEO_REQUIRED")
        for rows, key in ((self.assets, "asset_id"), (self.sentences, "sentence_id"),
                          (tuple(self.videos) + tuple(self.audios) + tuple(self.subtitles), "event_id")):
            ids = [getattr(row, key) for row in rows]
            for value in ids:
                _text(value, key)
            _require(len(set(ids)) == len(ids), f"DUPLICATE_ID:{key}")
        assets, sentences = self.asset_map(), self.sentence_map()
        for a in self.assets:
            _text(a.path, "asset.path")
            _require(a.kind in {"video", "audio"}, "ASSET_KIND_UNSUPPORTED")
            _int(a.duration_us, "asset.duration_us", 1)
            for name in ("width", "height", "fps_num"):
                _int(getattr(a, name), "asset." + name)
            _int(a.fps_den, "asset.fps_den", 1)
            if a.origin_asset_id is not None:
                _require(a.origin_asset_id in assets and a.origin_asset_id != a.asset_id,
                         "ASSET_ORIGIN_INVALID")
        def span(start, duration, name):
            _int(start, name + ".start")
            _int(duration, name + ".duration", 1)
            _require(start + duration <= self.duration_us, f"TARGET_OUT_OF_BOUNDS:{name}")
        def refs(ids, name):
            _require(isinstance(ids, (list, tuple)) and len(set(ids)) == len(ids),
                     f"SENTENCE_IDS_INVALID:{name}")
            _require(all(s in sentences for s in ids), f"SENTENCE_REFERENCE_INVALID:{name}")
        audio_map = {a.event_id: a for a in self.audios}
        for s in self.sentences:
            _text(s.text, "sentence.text")
            span(s.start_us, s.duration_us, s.sentence_id)
            if s.narration_id is not None:
                a = audio_map.get(s.narration_id)
                _require(a is not None and a.role == "narration" and s.sentence_id in a.sentence_ids,
                         "NARRATION_REFERENCE_INVALID")
                _require(a.target_start_us == s.start_us and a.duration_us == s.duration_us,
                         "NARRATION_SENTENCE_TIMING_MISMATCH")
        ordered = sorted(self.sentences, key=lambda s: s.start_us)
        _require(all(a.start_us + a.duration_us <= b.start_us for a, b in zip(ordered, ordered[1:])),
                 "SENTENCE_OVERLAP")
        cursor = 0
        for v in self.videos:
            span(v.target_start_us, v.duration_us, v.event_id)
            refs(v.sentence_ids, v.event_id)
            associated = {s.sentence_id for s in self.sentences if
                          max(s.start_us, v.target_start_us) < min(s.start_us + s.duration_us,
                                                                v.target_start_us + v.duration_us)}
            _require(set(v.sentence_ids) == associated, "VIDEO_SENTENCE_TIMING_MISMATCH")
            _require(v.target_start_us == cursor, "VIDEO_GAP_OR_OVERLAP")
            cursor += v.duration_us
            a = assets.get(v.asset_id)
            _require(a is not None and a.kind == "video", "VIDEO_ASSET_INVALID")
            _int(v.source_start_us, "video.source_start_us")
            _int(v.source_duration_us, "video.source_duration_us", 1)
            _require(v.source_start_us + v.source_duration_us <= a.duration_us, "SOURCE_OUT_OF_BOUNDS")
            _int(v.speed_num, "speed_num", 1)
            _int(v.speed_den, "speed_den", 1)
            _require(abs(v.source_duration_us * v.speed_den - v.duration_us * v.speed_num)
                     <= max(v.speed_num, v.speed_den), "SPEED_DURATION_MISMATCH")
            _require(self.video_frame_range(v)[1] > self.video_frame_range(v)[0], "VIDEO_BELOW_ONE_FRAME")
            layout = v.layout
            _require(layout.mode in {"cover", "contain"} and layout.opacity == 1.0, "LAYOUT_UNSUPPORTED")
            for name in ("scale", "offset_x", "offset_y", "opacity"):
                _number(getattr(layout, name), "layout." + name)
            _require(layout.scale >= 1 and abs(layout.offset_x) <= 1 and abs(layout.offset_y) <= 1,
                     "LAYOUT_RANGE_INVALID")
            for p in v.protected_intervals:
                _text(p.interval_id, "interval_id")
                _int(p.source_start_us, "protected.start")
                _int(p.source_end_us, "protected.end", 1)
                _int(p.min_output_frames, "protected.min_output_frames", 1)
                _require(v.source_start_us <= p.source_start_us < p.source_end_us <=
                         v.source_start_us + v.source_duration_us, "PROTECTED_INTERVAL_OUT_OF_WINDOW")
        _require(cursor == self.duration_us, "VIDEO_TOTAL_MISMATCH")
        for a in self.audios:
            span(a.target_start_us, a.duration_us, a.event_id)
            refs(a.sentence_ids, a.event_id)
            _require(a.asset_id in assets, "AUDIO_ASSET_INVALID")
            _require(a.role in {"narration", "bgm", "original"}, "AUDIO_ROLE_UNSUPPORTED")
            _require(assets[a.asset_id].kind == "audio" or a.role == "original", "AUDIO_KIND_INVALID")
            _int(a.source_start_us, "audio.source_start_us")
            _require(a.source_start_us + a.duration_us <= assets[a.asset_id].duration_us,
                     "AUDIO_SOURCE_OUT_OF_BOUNDS")
            _number(a.gain, "audio.gain", 0)
            for f in (a.fade_in_us, a.fade_out_us):
                _int(f, "audio.fade")
            _require(a.fade_in_us + a.fade_out_us <= a.duration_us, "AUDIO_FADE_OUT_OF_BOUNDS")
        for sub in self.subtitles:
            _require(sub.sentence_id in sentences, "SUBTITLE_SENTENCE_INVALID")
            _text(sub.text, "subtitle.text")
            span(sub.start_us, sub.duration_us, sub.event_id)
            s = sentences[sub.sentence_id]
            _require(s.start_us <= sub.start_us and sub.start_us + sub.duration_us <= s.start_us + s.duration_us,
                     "SUBTITLE_SENTENCE_TIMING_MISMATCH")
            style = sub.style
            _text(style.font_name, "font_name")
            _require(not any(x in style.font_name for x in (",", "\n", "\r")), "FONT_NAME_INVALID")
            _number(style.font_size_px, "font_size_px", 1)
            _require(type(style.bold) is bool, "SUBTITLE_BOLD_INVALID")
            _require(isinstance(style.color, str) and len(style.color) == 7 and style.color[0] == "#"
                     and all(x in "0123456789ABCDEFabcdef" for x in style.color[1:]), "SUBTITLE_COLOR_INVALID")
            for p in (style.x, style.y):
                _number(p, "subtitle.position")
                _require(-1 <= p <= 1, "SUBTITLE_POSITION_INVALID")
        cues = sorted(self.subtitles, key=lambda sub: sub.start_us)
        _require(all(a.start_us + a.duration_us <= b.start_us for a, b in zip(cues, cues[1:])),
                 "SUBTITLE_OVERLAP")
        # Also reject non-JSON/non-finite evidence. These facts carry no acceptance authority.
        try:
            json.dumps(asdict(self), allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise TimelineError("TIMELINE_NOT_JSON") from exc
        return self

    def to_dict(self):
        self.validate()
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @classmethod
    def from_dict(cls, data):
        def build(kind, raw, nested=None):
            _require(isinstance(raw, dict), f"OBJECT_REQUIRED:{kind.__name__}")
            unknown = set(raw) - {f.name for f in fields(kind)}
            _require(not unknown, f"UNKNOWN_FIELDS:{kind.__name__}:{','.join(sorted(unknown))}")
            values = dict(raw)
            for key, target in (nested or {}).items():
                if key not in values:
                    continue
                if isinstance(target, list):
                    _require(isinstance(values[key], (list, tuple)), f"ARRAY_REQUIRED:{key}")
                    values[key] = tuple(target[0](v) for v in values[key])
                else:
                    values[key] = target(values[key])
            for key in ("sentence_ids", "requirements", "evidence", "unresolved", "capabilities"):
                if key in values:
                    _require(isinstance(values[key], (list, tuple)), f"ARRAY_REQUIRED:{key}")
                    values[key] = tuple(values[key])
            try:
                return kind(**values)
            except TypeError as exc:
                raise TimelineError(f"INVALID_FIELDS:{kind.__name__}:{exc}") from exc
        value = build(cls, data, {
            "canvas": lambda v: build(Canvas, v), "assets": [lambda v: build(Asset, v)],
            "sentences": [lambda v: build(Sentence, v)],
            "videos": [lambda v: build(VideoEvent, v, {"layout": lambda v: build(VideoLayout, v),
                         "protected_intervals": [lambda v: build(SourceInterval, v)]})],
            "audios": [lambda v: build(AudioEvent, v)],
            "subtitles": [lambda v: build(SubtitleEvent, v, {"style": lambda v: build(SubtitleStyle, v)})]})
        return value.validate()

    def save(self, path):
        path = Path(path)
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))
