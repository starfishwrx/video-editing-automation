"""Compile observed event boundaries into editing policy, independently of narration length."""
import re
from copy import deepcopy

from .experiment_retiming import validate_motion,windows,fit_group,boundary


def allows_absorption_focus(text):
    return bool(text) and not re.search(r'秒|瞬吸|瞬间|立刻|马上|一倒就|倒|整杯|毫升|ml', text, re.I)


def validate_focus(sentences,roles,observations):
    """A partial pour supports qualitative absorption, never a complete/timed pour claim."""
    if (not isinstance(observations,list) or len(observations)!=2 or not isinstance(observations[0],dict)
            or not isinstance(observations[0].get('segments'),list)):
        raise ValueError('EXPERIMENT_MOTION_MEMBER_COVERAGE')
    role_map={r['sentence_id']:r['role'] for r in roles}
    text=''.join(s.text for s in sentences if role_map[s.sentence_id]=='absorption')
    focused=observations[0].get('coverage')=='absorption_focus'
    timed_process_faster=any(s['kind'] in {'pour','absorb'} and s['can_speed'] for s in observations[0]['segments'])
    if timed_process_faster and re.search(r'秒|瞬吸|瞬间|立刻|马上|一倒就',text):
        raise ValueError('EXPERIMENT_ABSORPTION_SPEED_INCOMPATIBLE_CLAIM')
    if focused and not allows_absorption_focus(text):
        raise ValueError('EXPERIMENT_ABSORPTION_FOCUS_INCOMPATIBLE_CLAIM')


def compile_events(row,member):
    """The observer states visible facts; this function chooses speedable middle phases."""
    if not isinstance(row,dict) or row.get('verdict')!='observed':
        raise ValueError('EXPERIMENT_EVENTS_UNCERTAIN')
    events=row.get('events');window=row.get('effective_window_frame_ids')
    if not isinstance(events,list) or not all(isinstance(e,dict) for e in events):
        raise ValueError('EXPERIMENT_EVENTS_CHAIN_REQUIRED')
    # An observer may describe the immediately preceding preparation while
    # declaring a selected window that already starts at actual contact.
    if (member['part']=='B' and isinstance(window,list) and len(window)==2 and isinstance(events,list)
            and events and events[0].get('kind')=='context'):
        context=events[0]
        if (type(context.get('first_frame'))is int and type(context.get('last_frame'))is int
                and member['window_frame_ids'][0]<=context['first_frame']<=context['last_frame']==window[0]-1):
            events=events[1:]
    names=('pour','absorb') if member['part']=='A' else ('press','lift','result')
    if member['part']=='B' and isinstance(events,list) and events and events[0].get('kind')=='context':
        names=('context',)+names
    if not isinstance(events,list) or [e.get('kind') for e in events]!=list(names):
        raise ValueError('EXPERIMENT_EVENTS_CHAIN_REQUIRED')
    if not isinstance(window,list) or len(window)!=2:raise ValueError('EXPERIMENT_EVENTS_WINDOW_REQUIRED')
    if member['part']=='B':
        by_kind={e['kind']:e for e in events}
        for name,kind in (('contact_first_frame','press'),('release_first_frame','lift'),('result_first_frame','result')):
            if type(row.get(name))is not int or row[name]!=by_kind[kind].get('first_frame'):
                raise ValueError('EXPERIMENT_EVENTS_CONTACT_RELEASE_MARKER_REQUIRED')
    cursor=window[0];segments=[]
    for event in events:
        a,b=event.get('first_frame'),event.get('last_frame');kind=event['kind'];fact=event.get('observed')
        if (type(a)is not int or type(b)is not int or a!=cursor or b<a or b>window[1]
                or not isinstance(fact,str) or not fact.strip()):
            raise ValueError('EXPERIMENT_EVENTS_COVERAGE_INVALID')
        # Use source PTS: original speed alone does not make a one-frame
        # absorption result readable. Reject a short state, never pad/freeze it.
        tail=2 if kind=='press' else 1
        if kind=='absorb':
            end=boundary(member['samples'],b+1,window[1])
            if end-boundary(member['samples'],a,window[1])<300_000:
                raise ValueError('EXPERIMENT_ABSORPTION_STATE_TOO_SHORT')
            first=b
            while end-boundary(member['samples'],first,window[1])<300_000:first-=1
            tail=b-first+1
        if kind=='context':spans=[(a,b,True)]
        elif kind in {'lift','result'} or b-a+1<=tail+1:
            spans=[(a,b,False)]
        else:spans=[(a,a,False),(a+1,b-tail,True),(b-tail+1,b,False)]
        segments.extend({'kind':kind,'first_frame':x,'last_frame':y,'can_speed':speed,'observed':fact}
            for x,y,speed in spans)
        cursor=b+1
    if cursor!=window[1]+1:raise ValueError('EXPERIMENT_EVENTS_COVERAGE_INVALID')
    observation={'verdict':'observed','coverage':row.get('coverage'),
        'effective_window_frame_ids':window,'segments':segments,'limitations':row.get('limitations','')}
    if member['part']=='A':observation['minimum_absorb_state_us']=300_000
    return validate_motion(observation,member)


def fit_phase_windows(members,observations,result_frames,target_us,phase_boundary_us,*,fps=30):
    """Select a continuous absorption-focused tail, then solve each clause's budget.

    This never omits an internal interval or claims to show the full pour.
    Actual observed phase boundaries remain unchanged; only the source in-point
    may advance inside the already observed pour, retaining >=400 ms of flow.
    """
    windows(members,observations,result_frames,fps=fps)
    observed=deepcopy(observations)
    b_selection=None
    if phase_boundary_us is not None and observed[1]['segments'][0]['kind']=='context':
        b_row=observed[1];initial=b_row['effective_window_frame_ids'][0]
        b_row['segments']=b_row['segments'][1:];start=b_row['segments'][0]['first_frame']
        if b_row['segments'][0]['kind']!='press':raise ValueError('EXPERIMENT_PHASE_PRESS_ANCHOR_REQUIRED')
        b_row['effective_window_frame_ids'][0]=start
        b_selection={'coverage':'complete_verification','omitted_preparation_frame_ids':[initial,start-1],
            'selected_window_frame_ids':b_row['effective_window_frame_ids'],'internal_omissions':False}
    clips=windows(members,observed,result_frames,fps=fps)
    if phase_boundary_us is None or observations[0].get('coverage')!='absorption_focus':
        plans=fit_group(clips,target_us,fps=fps,phase_boundary_us=phase_boundary_us)
        if b_selection:plans[1]['source_selection']=b_selection
        return plans
    if not 0<phase_boundary_us<target_us:raise ValueError('EXPERIMENT_PHASE_BOUNDARY_INVALID')
    b=fit_group([clips[1]],target_us-phase_boundary_us,fps=fps)
    if b_selection:b[0]['source_selection']=b_selection
    original=observations[0];first=original['effective_window_frame_ids'][0]
    last=max(s['last_frame'] for s in original['segments'] if s['kind']=='pour')-3
    for cap in (2,3):
        for start in range(first,last+1):
            cropped=deepcopy(original);cropped['effective_window_frame_ids'][0]=start
            cropped['segments']=[{**s,'first_frame':max(start,s['first_frame'])}
                for s in original['segments'] if s['last_frame']>=start]
            head=cropped['segments'][0]
            if head['kind']!='pour':continue
            if head['can_speed']:
                if head['last_frame']>start:
                    cropped['segments'].insert(1,{**head,'first_frame':start+1})
                head.update(last_frame=start,can_speed=False)
            clip=windows(members,[cropped,observed[1]],result_frames,fps=fps)[0]
            try:a=fit_group([clip],phase_boundary_us,fps=fps)
            except ValueError:continue
            if max(p['speed'] for p in a[0]['retiming']['parts'])>cap:continue
            a[0]['source_selection']={'coverage':'absorption_focus','observed_window_frame_ids':original['effective_window_frame_ids'],
                'selected_window_frame_ids':cropped['effective_window_frame_ids'],
                'omitted_pour_prefix_frame_ids':[first,start-1] if start>first else [],'internal_omissions':False}
            return a+b
    raise ValueError(f'EXPERIMENT_PHASE_DURATION_CONFLICT:part=0;budget_us={phase_boundary_us};3X_CAP')
