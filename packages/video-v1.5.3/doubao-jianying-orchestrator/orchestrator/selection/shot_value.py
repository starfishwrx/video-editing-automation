"""Judge what the shot communicates, separately from readability or motion amount."""


def assess(value,request,bounds):
    if not isinstance(value,dict) or not isinstance(value.get('claims'),list):
        raise ValueError('SHOT_VALUE_CLAIMS_REQUIRED')
    expected={c['id']:c.get('claim_text',request['text']) for c in request['checks']}
    if not expected:raise ValueError('SHOT_VALUE_CLAIMS_REQUIRED')
    if any(not isinstance(text,str) or not text.strip() or text not in request['text'] for text in expected.values()):
        raise ValueError('SHOT_VALUE_REQUEST_CLAIM_SUBSTITUTED')
    from .opening_hook import is_packaging_hook, assess_packaging
    if is_packaging_hook(request):
        return assess_packaging(value, request, bounds)
    omitted=value.get('uncovered_spoken_claims',[])
    if (value.get('all_spoken_claims_checked') is not None and type(value['all_spoken_claims_checked'])is not bool
            or not isinstance(omitted,list)
            or any(not isinstance(text,str) or not text.strip() or text not in request['text'] for text in omitted)):
        raise ValueError('SHOT_VALUE_FULL_SENTENCE_REVIEW_REQUIRED')
    rows=value['claims']
    if (any(not isinstance(r,dict) for r in rows)
            or len({r.get('check_id') for r in rows})!=len(rows)
            or not {r.get('check_id') for r in rows}<=set(expected)):
        raise ValueError('SHOT_VALUE_CLAIM_COVERAGE')
    supported=[];complete=[];evidence=[];strength=0;reasons=[]
    for row in rows:
        if row.get('claim_text')!=expected[row['check_id']]:raise ValueError('SHOT_VALUE_CLAIM_SUBSTITUTED')
        role=row.get('role');coverage=row.get('coverage')
        if (role not in {'direct_demonstration','feature_presentation','general_product','generic_motion','unrelated','uncertain'}
                or coverage not in {'full','partial','none'} or type(row.get('adds_claim_information'))is not bool
                or type(row.get('specific_link'))is not bool
                or any(not isinstance(row.get(k),str) or not row[k].strip() for k in
                    ('action_or_presentation','visible_change_or_feature','claim_link','missing_information'))):
            raise ValueError('SHOT_VALUE_FACTS_REQUIRED')
        spans=row.get('evidence_intervals_us')
        if not isinstance(spans,list):raise ValueError('SHOT_VALUE_EVIDENCE_REQUIRED')
        for span in spans:
            if (not isinstance(span,dict) or type(span.get('start'))is not int or type(span.get('end'))is not int
                    or not bounds['start']<=span['start']<span['end']<=bounds['end']):
                raise ValueError('SHOT_VALUE_EVIDENCE_OUTSIDE_WINDOW')
        generic=request.get('display_goal')=='general_product' and role=='general_product'
        useful=(coverage!='none' and spans and ((role in {'direct_demonstration','feature_presentation'}
            and row['adds_claim_information'] and row['specific_link']) or generic))
        if useful:
            supported.append(row['check_id']);evidence.extend(spans)
            if coverage=='full':complete.append(row['check_id'])
            strength=max(strength,3 if role=='direct_demonstration' else 2 if role=='feature_presentation' else 1)
        else:reasons.append({'check_id':row['check_id'],'role':role,'reason':row['claim_link']})
    status=('FULL' if len(complete)==len(expected) and value.get('all_spoken_claims_checked') and not omitted
        else 'PARTIAL' if supported else 'NO_SELLING_POINT_INFORMATION')
    return {'status':status,'supported_ids':supported,'fully_supported_ids':complete,
        'uncovered_ids':[i for i in expected if i not in complete],
        'all_spoken_claims_checked':value.get('all_spoken_claims_checked'),'uncovered_spoken_claims':omitted,
        'selection_eligible':bool(supported),'selection_policy':'at_least_one_concrete_selling_point',
        'strength':strength,'evidence_intervals_us':evidence,'reasons':reasons}


def rank(candidate):
    value=candidate.get('plan',{}).get('selling_point_value') or {}
    return (-value.get('strength',0),-len(value.get('supported_ids',[])))
