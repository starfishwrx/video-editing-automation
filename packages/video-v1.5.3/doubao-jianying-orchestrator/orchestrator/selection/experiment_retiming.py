"""Fit verified experiment phases, keeping contact, completion and results real-time."""
from fractions import Fraction
import math

from .paper_retiming import _allocate, _protected, plan_retiming


def boundary(samples,index,window_end):
    if index==window_end+1:
        return min(samples['source_end_us'],samples['frames'][window_end]['pts_us']+16_667)
    return samples['frames'][index]['pts_us']


def validate_motion(row,member):
    if not isinstance(row,dict)or row.get('verdict')not in {'observed','uncertain'}:
        raise ValueError('EXPERIMENT_MOTION_VERDICT_INVALID')
    if row['verdict']!='observed':raise ValueError('EXPERIMENT_MOTION_UNCERTAIN')
    coverage=row.get('coverage','complete_pour' if member['part']=='A' else 'complete_verification')
    if coverage not in ({'complete_pour','absorption_focus'} if member['part']=='A' else {'complete_verification'}):
        raise ValueError('EXPERIMENT_MOTION_COVERAGE_INVALID')
    left,right=member['window_frame_ids'];window=row.get('effective_window_frame_ids')
    if (not isinstance(window,list)or len(window)!=2 or any(type(i)is not int for i in window)
            or not left<=window[0]<window[1]<=right):
        raise ValueError('EXPERIMENT_MOTION_WINDOW_OUTSIDE_REVIEW')
    segments=row.get('segments');cursor=window[0];seen={}
    allowed={'pour','absorb','context'}if member['part']=='A'else {'press','lift','result','context'}
    if not isinstance(segments,list)or not segments or not isinstance(row.get('limitations'),str):
        raise ValueError('EXPERIMENT_MOTION_SEGMENTS_REQUIRED')
    for segment in segments:
        if not isinstance(segment,dict):raise ValueError('EXPERIMENT_MOTION_SEGMENT_INVALID')
        a,b=segment.get('first_frame'),segment.get('last_frame');kind=segment.get('kind')
        if (type(a)is not int or type(b)is not int or a!=cursor or not a<=b<=window[1]
                or kind not in allowed or type(segment.get('can_speed'))is not bool
                or not isinstance(segment.get('observed'),str)or not segment['observed'].strip()):
            raise ValueError('EXPERIMENT_MOTION_COVERAGE_OR_FACT_INVALID')
        if kind in {'lift','result'}and segment['can_speed']:
            raise ValueError('EXPERIMENT_MOTION_REQUIRED_PHASE_MUST_STAY_REALTIME')
        seen.setdefault(kind,[]).append(segment);cursor=b+1
    required={'pour','absorb'}if member['part']=='A'else {'press','lift','result'}
    if cursor!=window[1]+1 or not required<=seen.keys():
        raise ValueError('EXPERIMENT_MOTION_COMPLETE_CHAIN_REQUIRED')
    for kind in required:
        if seen[kind][0]['can_speed']or seen[kind][-1]['can_speed']:
            raise ValueError('EXPERIMENT_MOTION_CONTACT_OR_COMPLETION_UNPROTECTED')
    ordered=('pour','absorb')if member['part']=='A'else ('press','lift','result')
    if any(seen[a][-1]['last_frame']>=seen[b][0]['first_frame']for a,b in zip(ordered,ordered[1:])):
        raise ValueError('EXPERIMENT_MOTION_PHASE_ORDER_INVALID')
    return row


def windows(members,observations,result_frames,*,fps=30):
    result=[]
    if not isinstance(observations,list)or len(observations)!=len(members):
        raise ValueError('EXPERIMENT_MOTION_MEMBER_COVERAGE')
    for member,row in zip(members,observations):
        validate_motion(row,member);samples=member['samples'];a,b=row['effective_window_frame_ids']
        protected=[]
        state_min=row.get('minimum_absorb_state_us',300_000 if member['part']=='A' and row.get('coverage') else None)
        if state_min is not None and (member['part']!='A' or type(state_min)is not int or state_min!=300_000):
            raise ValueError('EXPERIMENT_ABSORPTION_STATE_MINIMUM_INVALID')
        state=next((s for s in reversed(row['segments']) if s['kind']=='absorb'),None)
        for segment in row['segments']:
            if not segment['can_speed']:
                start=boundary(samples,segment['first_frame'],b);end=boundary(samples,segment['last_frame']+1,b)
                required=math.ceil(state_min*fps/1_000_000) if state_min and segment is state else 1
                if state_min and segment is state and end-start<state_min:
                    raise ValueError('EXPERIMENT_ABSORPTION_STATE_TOO_SHORT')
                protected.append({'start':start,'end':end,
                    'kind':'result'if segment['kind']=='result'else'action',
                    'min_output_frames':result_frames if segment['kind']=='result'else required})
        if any(p['kind']=='result'and (p['end']-p['start'])*fps<result_frames*1_000_000 for p in protected):
            raise ValueError('EXPERIMENT_RESULT_WINDOW_TOO_SHORT')
        result.append({'source_start_us':boundary(samples,a,b),'source_end_us':boundary(samples,b+1,b),
            'protected_intervals':protected})
    return result


def fit_group(clips,target_us,*,fps=30,phase_boundary_us=None,max_speed=3.0):
    """Fit without cutting footage, respecting the task and production speed caps."""
    if type(max_speed) not in (int, float) or not math.isfinite(max_speed) or not 1 <= max_speed <= 3:
        raise ValueError('EXPERIMENT_SPEED_LIMIT_INVALID')
    if phase_boundary_us is not None:
        if len(clips)!=2 or not 0<phase_boundary_us<target_us:
            raise ValueError('EXPERIMENT_PHASE_BOUNDARY_INVALID')
        result=[]
        for index,(clip,budget) in enumerate(zip(clips,(phase_boundary_us,target_us-phase_boundary_us))):
            try:result.extend(fit_group([clip],budget,fps=fps,max_speed=max_speed))
            except ValueError as error:
                raise ValueError(f'EXPERIMENT_PHASE_DURATION_CONFLICT:part={index};budget_us={budget};{error}') from error
        return result
    lengths=[];owners=[];locked=[]
    for i,clip in enumerate(clips):
        start,end=clip['source_start_us'],clip['source_end_us']
        protected=_protected(clip['protected_intervals'],start,end)
        if protected is None:raise ValueError('EXPERIMENT_MOTION_PROTECTION_INVALID')
        locked.append(sum(b-a for a,b in protected));cursor=start
        for a,b in protected+[(end,end)]:
            if a>cursor:lengths.append(a-cursor);owners.append(i)
            cursor=b
    free=target_us-sum(locked);allocated=None;cap=1
    if lengths:
        for cap in dict.fromkeys((min(2,max_speed),max_speed)):
            allocated=_allocate(lengths,free,Fraction(str(cap)))
            if allocated is not None:break
        if allocated is None:
            raise ValueError('EXPERIMENT_DURATION_CONFLICT_WITH_3X_CAP' if max_speed == 3
                             else 'EXPERIMENT_DURATION_CONFLICT_WITH_TASK_CAP')
    elif free!=0:raise ValueError('EXPERIMENT_DURATION_CONFLICT_NO_COMPRESSIBLE_PROCESS')
    budgets=list(locked)
    for owner,duration in zip(owners,allocated or []):budgets[owner]+=duration
    fitted=[]
    for clip,budget in zip(clips,budgets):
        plan=plan_retiming(clip['source_start_us'],clip['source_end_us'],clip['protected_intervals'],budget,
            fps=fps,preferred_cap=min(2,max_speed),hard_cap=max_speed)
        if not plan['ok']:raise ValueError('EXPERIMENT_'+plan['reason'])
        fitted.append({**clip,'target_duration_us':budget,'retiming':plan})
    return fitted
