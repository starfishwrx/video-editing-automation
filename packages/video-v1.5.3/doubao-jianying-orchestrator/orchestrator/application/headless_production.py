"""Default writer adapter for already selected, voiced and aligned clips."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import unicodedata
import uuid

from ..audio import audio_captions
from ..audio.narration_probe import probe_audio
from ..media.probe import probe_media
from ..policy.production_parameters import subtitle_transform
from ..runtime.draft_documents import native_backend
from ..runtime.platform_env import find_drafts_root
from ..quality import draft_safety
from .engine import BRIGHTNESS_KEYFRAME_VALUE


def _us(value):
    return round(float(value) * 1_000_000)


def _text(value):
    return ''.join(c for c in value if not unicodedata.category(c).startswith('P'))


def make_plan(engine, clips, bgm_path, overrides, cues, name, font_path):
    """Pure cut mapping; no selection, TTS, home registration or QC promotion."""
    for key in ('effects', 'filters', 'adjustments', 'subtitle_style', 'video_layout', 'canvas'):
        if engine.m.get(key):
            raise ValueError('HEADLESS_PRODUCTION_OPTION_UNSUPPORTED:' + key)
    if engine.m.get('aspect_ratio', '9:16') not in {'9:16', '16:9'}:
        raise ValueError('HEADLESS_ASPECT_RATIO_UNSUPPORTED')
    width, height = (1080, 1920) if engine.m.get('aspect_ratio', '9:16') == '9:16' else (1920, 1080)
    position = subtitle_transform(engine.m, width, height)
    brightness = engine.m.get('brightness')
    if brightness is None:
        brightness = BRIGHTNESS_KEYFRAME_VALUE
    brightness = float(brightness.get('value', BRIGHTNESS_KEYFRAME_VALUE) if isinstance(brightness, dict) else brightness)
    if not -1 <= brightness <= 1:
        raise ValueError('HEADLESS_BRIGHTNESS_INVALID')
    videos, narrations, captions, built = [], [], [], []
    audio_groups = {}
    facts = {}
    def probe(path):
        key = str(Path(path).resolve())
        if key not in facts:
            facts[key] = probe_media(key, ffprobe=str(engine.ffprobe or 'ffprobe'))
        return facts[key]
    for c in clips:
        span, speed = _us(c.duration), float(c.video_speed or 1)
        source = str(Path(getattr(engine, '_uploaded_videos', {}).get(str(Path(c.video).resolve()), c.video)).resolve())
        video = {'source': source, 'start_us': c.start_us, 'duration_us': span,
                 'source_start_us': _us(c.source_start), 'source_duration_us': round(span * speed),
                 'speed': speed, 'volume': 1 if c.audio_mode == 'native' else 0, 'brightness': brightness}
        # Preserve requested transitions; unsupported effects fail explicitly in
        # the backend instead of silently dropping a production rule.
        if c.transition:
            if not videos or c.transition.get('type') not in {'叠化', 'dissolve'}:
                raise ValueError('HEADLESS_TRANSITION_UNSUPPORTED')
            videos[-1]['transition_out'] = {'name': 'dissolve', 'duration_us': _us(c.transition['duration']),
                                            'edge_policy': 'require-handles'}
        videos.append(video)
        narration = c.tts_path if c.audio_mode == 'tts' else c.audio if c.audio_mode == 'file' else None
        if narration:
            audio = str(Path(overrides.get(str(Path(narration)), narration)).resolve())
            offset = engine._audio_slice_us(c)
            available = probe(audio)['duration_us'] - offset
            if available <= 0 or span - available > 160_000:
                raise ValueError('HEADLESS_NARRATION_SLICE_OUT_OF_BOUNDS')
            audio_span = min(span, available)
            parent = getattr(c, 'parent_index', -1)
            group = ('parent', parent) if parent >= 0 else ('clip', len(built))
            audio_groups.setdefault(group, []).append((audio, offset, audio_span, c.start_us))
            narrations.append({'source': audio, 'start_us': c.start_us, 'duration_us': audio_span,
                               'source_start_us': offset, 'source_duration_us': min(span, available), 'volume': 1})
        built.append({'index': len(built), 'start_us': c.start_us, 'duration_us': span,
                      'source_start': c.source_start, 'audio_mode': c.audio_mode, 'text': c.text,
                      'transition': c.transition, 'stability': c.stability,
                      'visual_missing': bool(c.visual_missing), 'operator_note': c.visual_missing_note})
    # Full sentence coverage is stricter than the old one-frame timing
    # tolerance. A short video must never silently consume the end of speech.
    for slices in audio_groups.values():
        path, _, _, origin = slices[0]
        cursor = 0
        for audio, offset, span, target in slices:
            if audio != path or offset != cursor or target != origin + offset:
                raise ValueError('HEADLESS_NARRATION_SLICES_DISCONTINUOUS')
            cursor += span
        if cursor != probe(path)['duration_us']:
            raise ValueError('HEADLESS_NARRATION_DURATION_CONFLICT')
    for cue in cues:
        text = _text(cue['text'])
        if not text.strip():
            continue
        captions.append({'text': text, 'start_us': cue['start_us'], 'duration_us': cue['end_us'] - cue['start_us'],
                         'size': 8, 'bold': True, 'subtitle': True, 'x': position['x'], 'y': position['y'],
                         'color': '#FFFFFF', 'border_color': '#000000', 'border_width': .05,
                         'font_path': str(Path(font_path).resolve(strict=True))})
    tracks = [{'type': 'video', 'name': 'Video_BRoll', 'segments': videos}]
    if narrations:
        tracks.append({'type': 'audio', 'name': 'Narration', 'segments': narrations})
    if bgm_path:
        tracks.append({'type': 'audio', 'name': 'BGM', 'segments': [{'source': str(Path(bgm_path).resolve()),
                       'start_us': 0, 'duration_us': engine.total_us, 'source_start_us': 0,
                       'source_duration_us': engine.total_us, 'volume': 1}]})
    if captions:
        tracks.append({'type': 'text', 'name': 'Subtitles', 'segments': captions})
    return {'schema': 'jy14-headless-plan/v1', 'name': name,
            'canvas': {'width': width, 'height': height, 'fps': engine._fps()}, 'tracks': tracks}, built, position


def write_draft(engine, clips, bgm_path):
    if not bgm_path:
        return {'status': 'BGM_REQUIRED', 'draft_path': None, 'formal_delivery_allowed': False}
    gate = engine._invariant_gate(clips, stage='pre_write')
    if gate:
        return gate
    try:
        backend = native_backend()
        root = Path(find_drafts_root(engine.editor_root)).resolve()
        root.mkdir(parents=True, exist_ok=True)
        name = str(engine.m.get('draft_name') or '豆包剪映编排草稿')
        # Native production never overwrites an old or human-edited draft.
        if (root / name).exists():
            return {'status': 'HEADLESS_TARGET_EXISTS', 'draft_path': None, 'formal_delivery_allowed': False}
        overrides = engine._normalize_narrations(clips)
        source = str(engine.m.get('caption_source') or ('audio' if engine.m.get('selection_mode') == 'ai_editor' else 'legacy'))
        if source not in {'audio', 'legacy'} or (source == 'legacy' and engine.m.get('selection_mode') == 'ai_editor'):
            return {'status': 'CAPTION_CONFIG_INVALID', 'draft_path': None}
        caption = None
        if source == 'audio':
            def final_audio_probe(path):
                return probe_audio(path, ffprobe=str(engine.ffprobe or 'ffprobe'))
            caption = audio_captions.build_captions(engine.m, clips, engine.report_dir / 'captions',
                                                  audio_overrides=overrides, probe=final_audio_probe)
            if not caption.get('ok'):
                return {'status': 'CAPTION_TIMING_UNCERTIFIED', 'draft_path': None,
                        'formal_delivery_allowed': False, 'issues': caption.get('issues', [])}
            cues = caption['cues']
        else:
            cues = [{'text': c.text, 'start_us': c.start_us, 'end_us': c.start_us + _us(c.duration)}
                    for c in clips if c.text.strip()]
        font = engine.m.get('subtitle_font_path') or os.environ.get('JY_HEADLESS_FONT')
        if not font:
            raise ValueError('HEADLESS_SUBTITLE_FONT_REQUIRED')
        plan, built, position = make_plan(engine, clips, bgm_path, overrides, cues, name, font)
        work = engine.report_dir / ('headless-' + uuid.uuid4().hex[:10])
        work.mkdir(parents=True, exist_ok=False)
        plan_file = work / 'plan.json'
        plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        native = backend.build(plan_file, work / 'build', root)
        draft_dir = Path(native['draft_path'])
        if (engine.m.get('selection_mode') == 'ai_editor' or any(
                isinstance(s, dict) and s.get('selection_mode') == 'ai_editor' for s in engine.m.get('segments', []))):
            from .. import ai_editor_contract
            (draft_dir / 'ai_selection_identity.json').write_text(json.dumps({
                'schema': 'chatcut-ai-draft-v1', 'selection_mode': engine.m.get('selection_mode'),
                'selection_sha256': ai_editor_contract.digest(engine.m.get('segments', [])),
                'policy_version': ai_editor_contract.POLICY_VERSION,
                'editorial_rules': ai_editor_contract.rules_identity(engine.m)}, ensure_ascii=False, indent=2), encoding='utf-8')
        expected = {'video_segments': len(clips), 'narration_segments': sum(c.audio_mode == 'tts' and bool(c.tts_path)
                    or c.audio_mode == 'file' and bool(c.audio) for c in clips), 'subtitle_segments': len(cues),
                    'bgm_required': bool(bgm_path), 'bgm_segments': 1, 'subtitle_style_required': bool(cues),
                    'subtitle_font_size': 8, 'subtitle_bold': True, 'subtitle_transform_x': position['x'],
                    'subtitle_transform_y': position['y'], 'subtitle_strip_punctuation': True,
                    'native_subtitles_required': source == 'audio', 'subtitle_cues': cues if caption else None}
        issues = draft_safety.post_write_validate(draft_dir, expected_tracks=expected)
        if issues:
            return {'status': 'DRAFT_INVALID', 'draft_path': str(draft_dir), 'issues': issues,
                    'formal_delivery_allowed': False}
        if caption:
            (draft_dir / 'caption_identity.json').write_text(json.dumps({'schema': 'chatcut-audio-captions-v1',
                'identity': caption['identity'], 'cue_count': len(cues), 'ui_acceptance': 'UNKNOWN'},
                ensure_ascii=False, indent=2), encoding='utf-8')
        if bgm_path:
            (draft_dir / 'audio_identity.json').write_text(json.dumps({
                'schema': 'production-audio-v1', 'bgm_choice': getattr(engine, '_bgm_choice', None),
                'bgm_asset_sha256': hashlib.sha256(Path(bgm_path).read_bytes()).hexdigest(),
                'event_duration_us': engine.total_us}, ensure_ascii=False, indent=2), encoding='utf-8')
        result = engine._draft_result(clips, bgm_path, name, draft_dir, root, built, source, caption, engine.total_us)
        result.update(draft_backend='headless', headless_build=str(work / 'build'),
                      home_index_modified=False, native_ui_acceptance='pending')
        result['draft_open_guidance'] = ['使用交付草稿目录打开工程；素材引用已绑定交付位置。']
        return result
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError) as exc:
        return {'status': 'HEADLESS_BUILD_BLOCKED', 'reason': str(exc), 'draft_path': None,
                'formal_delivery_allowed': False, 'draft_backend': 'headless'}
