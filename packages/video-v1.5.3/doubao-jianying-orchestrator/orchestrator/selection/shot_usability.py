"""Readable visual information is required before a display shot can be ranked."""


def is_display(request):
    return (not request.get('require_action') and not request.get('require_result')
        and any(c.get('type')=='display' for c in request.get('checks',[])))


def assess(facts,request,window,*,reviewed_window=None,check_relation=True):
    if not isinstance(facts,dict):raise ValueError('SHOT_USABILITY_FACTS_REQUIRED')
    if (facts.get('view_kind') not in {'stable_display','complete_demonstration','handling_or_setup','uncertain'}
            or facts.get('subject_visibility') not in {'clear','partial','blocked','uncertain'}
            or (check_relation and facts.get('relation') not in {'visible_feature','general_product','inferred_only','unrelated','uncertain'})
            or type(facts.get('demonstration_complete'))is not bool
            or not isinstance(facts.get('information_point'),str) or not facts['information_point'].strip()
            or not isinstance(facts.get('limitations'),str)):
        raise ValueError('SHOT_USABILITY_FACTS_INVALID')
    intervals=facts.get('clear_intervals_us')
    if not isinstance(intervals,list):raise ValueError('SHOT_USABILITY_CLEAR_INTERVALS_REQUIRED')
    lo,hi=window['start'],window['end'];bounds=reviewed_window or window;spans=[]
    for row in intervals:
        if not isinstance(row,dict):raise ValueError('SHOT_USABILITY_INTERVAL_INVALID')
        a,b=row.get('start'),row.get('end')
        if (type(a)is not int or type(b)is not int or not bounds['start']<=a<b<=bounds['end']
                or not isinstance(row.get('observed'),str) or not row['observed'].strip()):
            raise ValueError('SHOT_USABILITY_INTERVAL_OUTSIDE_WINDOW')
        if max(a,lo)<min(b,hi):spans.append((max(a,lo),min(b,hi)))
    merged=[]
    for a,b in sorted(spans):
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
        else:merged.append((a,b))
    clear=sum(b-a for a,b in merged);longest=max((b-a for a,b in merged),default=0)
    reasons=[];goal=request.get('display_goal','visible_feature')
    if facts['subject_visibility'] in {'blocked','uncertain'}:reasons.append('SUBJECT_NOT_READABLE')
    if facts['view_kind'] in {'handling_or_setup','uncertain'}:reasons.append('HANDLING_WITHOUT_INFORMATION')
    if facts['view_kind']=='complete_demonstration' and not facts['demonstration_complete']:
        reasons.append('DEMONSTRATION_INCOMPLETE')
    if check_relation and (facts['relation'] in {'inferred_only','unrelated','uncertain'} or (goal!='general_product' and facts['relation']!='visible_feature')):
        reasons.append('REQUESTED_FEATURE_NOT_VISIBLE')
    if clear*2<hi-lo or longest<min(600_000,hi-lo):reasons.append('CLEAR_INFORMATION_TOO_BRIEF')
    return {'status':'USABLE_FOR_COMPARISON' if not reasons else 'REJECTED',
        'reasons':reasons,'clear_fraction':clear/(hi-lo),'longest_clear_us':longest,
        'subject_visibility':facts['subject_visibility'],'information_point':facts['information_point']}


def covers(facts,start,end):
    if start>=end:return True
    cursor=start
    for interval in sorted(facts.get('clear_intervals_us',[]),key=lambda r:r['start']):
        if interval['end']<=cursor:continue
        if interval['start']>cursor:return False
        cursor=max(cursor,interval['end'])
        if cursor>=end:return True
    return cursor>=end


def intersect_observations(blind,target,request,bounds):
    """The semantic observer may narrow a readable view, never widen it."""
    assess(blind,request,bounds,check_relation=False)
    assess(target,request,bounds)
    intervals=[]
    for a in blind['clear_intervals_us']:
        for b in target['clear_intervals_us']:
            start,end=max(a['start'],b['start']),min(a['end'],b['end'])
            if start<end:intervals.append({'start':start,'end':end,
                'observed':a['observed']+'; requested information: '+b['observed']})
    result={**blind,'relation':target['relation'],'information_point':blind['information_point'],
        'target_information_point':target['information_point'],
        'clear_intervals_us':intervals,'blind_observation':blind,'semantic_observation':target}
    if target['view_kind'] in {'handling_or_setup','uncertain'}:
        result['view_kind']=target['view_kind']
    if target['view_kind']=='complete_demonstration' and not target['demonstration_complete']:
        result.update(view_kind='complete_demonstration',demonstration_complete=False)
    if target['subject_visibility'] in {'blocked','uncertain'}:
        result['subject_visibility']=target['subject_visibility']
    return result


def rank(candidate):
    quality=candidate.get('usability') or candidate.get('plan',{}).get('usability') or {}
    return (candidate.get('comparison_rank',999),-quality.get('clear_fraction',0),
        quality.get('subject_visibility')!='clear',-quality.get('longest_clear_us',0))
