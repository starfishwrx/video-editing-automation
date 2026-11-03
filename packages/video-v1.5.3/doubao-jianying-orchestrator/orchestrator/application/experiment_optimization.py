"""Reproduce a reviewed experiment edit through the normal preview command."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import subprocess

from .. import ai_editor_contract as identity
from ..media.timeline_render import render_timeline
from ..quality.production_qc import check_production
from ..selection.experiment_contract import candidate_groups, contract_for, validate_group
from ..selection.experiment_retiming import windows
from ..vision.experiment_motion_review import PROTOCOL, validate as validate_motion
from ..vision.experiment_pair_review import input_binding
from ..vision.purpose_review import save
from .experiment_production import apply


def phase_spec_template(manifest, pair, motion=None):
    """Create editable review data tied to the exact source-frame cache."""
    if motion is not None:
        validate_motion(manifest, pair, motion)
    members = []
    for index, member in enumerate(pair['members']):
        observation = (deepcopy(motion['observations'][index]) if motion is not None else
            {'verdict': 'uncertain', 'effective_window_frame_ids': member['window_frame_ids'],
                'segments': [], 'limitations': 'Review the actual numbered frames before use.'})
        members.append({'part': member['part'], 'source': member['samples']['original']['path'],
            'reviewed_window_frame_ids': list(member['window_frame_ids']), 'observation': observation})
    return {'schema': 'experiment-phase-spec-v1', 'review_note': '', 'members': members,
        'pair_input_sha256': identity.digest(input_binding(manifest, pair['members'], pair['sheet']['path']))}


def bind_phase_spec(manifest, pair, spec, *, fps=30):
    """Bind explicitly reviewed source/phase data, without inventing an AI verdict."""
    if (not isinstance(spec, dict) or spec.get('schema') != 'experiment-phase-spec-v1'
            or not isinstance(spec.get('review_note'), str) or not spec['review_note'].strip()):
        raise ValueError('EXPERIMENT_PHASE_SPEC_REVIEW_REQUIRED')
    if spec.get('pair_input_sha256') != identity.digest(input_binding(manifest, pair['members'], pair['sheet']['path'])):
        raise ValueError('EXPERIMENT_PHASE_SPEC_REVIEW_INPUT_CHANGED')
    members = spec.get('members')
    if not isinstance(members, list) or len(members) != 2:
        raise ValueError('EXPERIMENT_PHASE_SPEC_MEMBER_COVERAGE')
    observations = []
    for declared, actual in zip(members, pair['members']):
        if (not isinstance(declared, dict) or declared.get('part') != actual['part']
                or not isinstance(declared.get('source'), str)
                or Path(declared['source']).resolve() != Path(actual['samples']['original']['path']).resolve()):
            raise ValueError('EXPERIMENT_PHASE_SPEC_SOURCE_CHANGED')
        if declared.get('reviewed_window_frame_ids') != actual['window_frame_ids']:
            raise ValueError('EXPERIMENT_PHASE_SPEC_REVIEW_INPUT_CHANGED')
        observations.append(deepcopy(declared.get('observation')))
    windows(pair['members'], observations, contract_for(manifest)['minimum_result_frames'], fps=fps)
    return {'protocol': PROTOCOL, 'task_id': manifest['task_id'], 'rules': identity.rules_identity(manifest),
        'pair_input_sha256': identity.digest(input_binding(manifest, pair['members'], pair['sheet']['path'])),
        'observations': observations, 'method': 'reviewed_phase_specification',
        'review_note': spec['review_note'], 'formal_delivery_allowed': False}


def _change_rows(before, after, sentence_ids):
    assets = after.asset_map()
    def shots(timeline, sentence):
        return [{'asset_id': v.asset_id, 'source_start_us': v.source_start_us,
            'source_end_us': v.source_start_us + v.source_duration_us,
            'target_start_us': v.target_start_us, 'duration_us': v.duration_us,
            'speed': v.speed_num / v.speed_den, 'source': assets[v.asset_id].path,
            'original_source': next((e['original']['path'] for e in v.evidence
                if isinstance(e, dict) and isinstance(e.get('original'), dict)
                and isinstance(e['original'].get('path'), str)), assets[v.asset_id].path)}
            for v in timeline.videos if sentence.sentence_id in v.sentence_ids]
    return [{'sentence_id': s.sentence_id, 'text': s.text, 'start_us': s.start_us,
        'duration_us': s.duration_us, 'before': shots(before, s), 'after': shots(after, s)}
        for s in before.sentences if s.sentence_id in sentence_ids]


def _phase_lags(timeline, roles, pair, motion, group_id):
    # This is a timing diagnostic, not another model's semantic acceptance.
    press = next(s for s in motion['observations'][1]['segments'] if s['kind'] == 'press')
    source_us = pair['members'][1]['samples']['frames'][press['first_frame']]['pts_us']
    event = next(v for v in timeline.videos if v.asset_id == group_id + '-1'
        and v.source_start_us <= source_us < v.source_start_us + v.source_duration_us)
    offset = Fraction((source_us - event.source_start_us) * event.speed_den, event.speed_num)
    press_us = event.target_start_us + (offset.numerator + offset.denominator - 1) // offset.denominator
    role_map = {r['sentence_id']: r['role'] for r in roles}
    selected = {sid for v in timeline.videos if v.asset_id in {group_id + '-0', group_id + '-1'}
        for sid in v.sentence_ids}
    return [{'sentence_id': s.sentence_id, 'sentence_start_us': s.start_us, 'press_start_us': press_us,
        'lag_us': press_us - s.start_us, 'status': 'PHASE_ALIGNMENT_REVIEW_REQUIRED'}
        for s in timeline.sentences if role_map[s.sentence_id] == 'rewet' and s.start_us < press_us
        and s.sentence_id in selected]


def _frame_execution(timeline, receipt, group_id):
    events = {v.event_id: v for v in timeline.videos}
    rows, steps, last = [], [], None
    fps = Fraction(timeline.canvas.fps_num, timeline.canvas.fps_den)
    for group in receipt['frame_mapping']:
        if not group['event_id'].startswith(group_id + '-'):
            continue
        event = events[group['event_id']]
        if any('bounded_process_speed_cap' in e for e in event.evidence):
            rows.append({'event_id': event.event_id, 'output_frames': len(group['frames']),
                'nominal_speed': event.speed_num / event.speed_den,
                'frame_budget_speed': float(Fraction(event.source_duration_us, 1_000_000) * fps / len(group['frames']))})
        for frame in group['frames']:
            if last is not None and last[0] == group['asset_id']:
                steps.append(float(Fraction(frame['source_pts_us'] - last[1], 1_000_000) * fps))
            last = group['asset_id'], frame['source_pts_us']
    return {'processes': rows, 'maximum_same_source_sample_step': max(steps, default=0),
        'protected_output_frames': [p for p in receipt['protected'] if p['event_id'].startswith(group_id + '-')]}


def optimize(manifest, timeline, roles, pair, prior_origins, directory, *, phase_spec=None,
             motion=None, sentence_ids=None, render=True, ffmpeg='ffmpeg', ffprobe='ffprobe', width=540):
    """Use fixed audio and cached evidence; never analyze a library or publish a draft."""
    if (phase_spec is None) == (motion is None):
        raise ValueError('EXPERIMENT_REQUIRES_ONE_MOTION_OR_PHASE_SPEC')
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    try:
        if roles.get('task_id') != manifest['task_id'] or roles.get('rules') != identity.rules_identity(manifest):
            raise ValueError('EXPERIMENT_ROLE_RULE_IDENTITY_CHANGED')
        groups = candidate_groups(timeline, roles['sentences'], contract_for(manifest))
        if sentence_ids is None:
            if len(groups) != 1:
                raise ValueError('EXPERIMENT_REQUIRES_ONE_UNAMBIGUOUS_GROUP')
            sentence_ids = groups[0]
        sentence_ids = tuple(sentence_ids)
        validate_group(timeline, sentence_ids, roles['sentences'], contract_for(manifest))
        if phase_spec is not None:
            motion = bind_phase_spec(manifest, pair, phase_spec,
                fps=timeline.canvas.fps_num / timeline.canvas.fps_den)
        validate_motion(manifest, pair, motion)
        group_id = 'experiment-optimization'
        updated = apply(manifest, timeline, sentence_ids, roles['sentences'], pair, group_id=group_id,
            prior_origins=prior_origins, motion=motion, ffprobe=ffprobe)
        selected = [s for s in timeline.sentences if s.sentence_id in sentence_ids]
        lo, hi = selected[0].start_us, selected[-1].start_us + selected[-1].duration_us
        outside = lambda t: tuple(v for v in t.videos if v.target_start_us >= hi or v.target_start_us + v.duration_us <= lo)
        preserved = {'sentences': updated.sentences == timeline.sentences, 'audio': updated.audios == timeline.audios,
            'subtitles': updated.subtitles == timeline.subtitles, 'outside_group_video': outside(updated) == outside(timeline)}
        if not all(preserved.values()):
            raise ValueError('EXPERIMENT_FIXED_INPUT_CHANGED')
        updated.save(root / 'timeline.json')
        save(root / 'motion.json', motion)
        changes = _change_rows(timeline, updated, sentence_ids)
        save(root / 'changes.json', changes)
        lags = _phase_lags(updated, roles['sentences'], pair, motion, group_id)
        result = {'status': 'PLANNED_OUTPUT_REVIEW_PENDING', 'formal_delivery_allowed': False,
            'task_id': manifest['task_id'], 'sentence_ids': sentence_ids, 'duration_us': hi - lo,
            'method': motion.get('method', 'unclassified_motion_artifact'), 'preserved': preserved,
            'phase_alignment': lags, 'timeline': str(root / 'timeline.json'),
            'limitations': ['自动阶段观察仍可能需要定向复核；连续实验组不保证每句卖点相位都已对齐。',
                '这是预览；整片观看和正式交付未认证。']}
        if render:
            receipt = render_timeline(updated, root / 'render', ffmpeg=ffmpeg, ffprobe=ffprobe,
                width=width, allow_real_tail=True)
            qc = check_production(updated, root / 'render', ffmpeg=ffmpeg, ffprobe=ffprobe)
            if qc['structure_media']['status'] != 'PASS':
                raise ValueError('EXPERIMENT_RENDER_STRUCTURE_FAILED')
            result.update(status='RENDERED_OUTPUT_REVIEW_PENDING', video=receipt['video'], qc=qc,
                execution=_frame_execution(updated, receipt, group_id))
        report = ['# 实验选镜与局部变速预览', '', '状态：' + result['status'],
            '正式交付未认证。配音、句长、字幕、BGM和组外画面保持原输入。', '',
            '| 句子 | 文案 | 原句长（秒） | 旧镜头数 | 优化后源窗口与计划倍速 |', '|---|---|---|---|---|']
        for row in changes:
            cuts = '; '.join(f"{Path(c['original_source']).name} {c['source_start_us']/1e6:.3f}–{c['source_end_us']/1e6:.3f}s @{c['speed']:.3f}x" for c in row['after'])
            report.append(f"| {row['sentence_id']} | {row['text'].replace('|', '/')} | {row['duration_us']/1e6:.6f} | {len(row['before'])} | {cuts.replace('|', '/')} |")
        report += ['', '## 仍需复核', '', *['- ' + s for s in result['limitations']]]
        report += [f"- {r['sentence_id']}：按压开始晚于本句起点 {r['lag_us']/1e6:.3f} 秒，需复核逐句画面对应。" for r in lags]
        report += ['', '计划倍速与整数帧实际执行分别记录，实际帧/倍速及检查结果见[result.json](result.json)。',
            '完整旧新源窗口见[changes.json](changes.json)。']
        (root / 'REPORT.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
        save(root / 'result.json', result)
        return result
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        save(root / 'result.json', {'status': 'BLOCKED', 'error': str(error), 'formal_delivery_allowed': False})
        raise
