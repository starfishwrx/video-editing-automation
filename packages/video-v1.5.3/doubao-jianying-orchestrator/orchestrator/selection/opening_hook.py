"""A packaging accompaniment is not proof of a spoken pain-point claim."""
from copy import deepcopy
import re

PURPOSE = 'opening_hook_packaging'
RULE = 'first_three_no_product_selling_point_packaging_v1'


def packaging_recall_priority(description):
    """Rank cache hints only; a high rank never certifies a packaging picture."""
    packaging = r'(?:包装|外盒|外袋|纸巾盒|洗脸巾盒|纸盒|纸箱|整箱|整提|整包|盒装|袋装|\bpackag(?:es?|ing)\b|\bcartons?\b)'
    clauses = re.split(r'[，,。；;！!？?\n]', description.lower())
    incidental = False
    for clause in clauses:
        if not re.search(packaging, clause):
            continue
        if re.search(r'(?:没有|未见|看不到|未出现|不含|无)[^，,。；;]{0,8}' + packaging
                     + r'|\b(?:no|without)\b.{0,20}' + packaging, clause):
            continue
        # "Against a white background" describes a backdrop, not a package
        # relegated to the background. Require an actual location relation.
        location = r'(?:背景(?:中|里|内|角落)?(?!前|墙|板)|角落|边角|边缘)'
        if re.search(location + r'(?:有|只有|仅有|可见|放着|摆着|露出|出现).{0,12}' + packaging
                     + '|' + packaging + r'.{0,6}(?:仅|只)?(?:在|位于|处于|放在|摆在|作为)' + location
                     + '|' + packaging + r'.{0,16}\b(?:in|at) (?:the )?(?:background|corner)\b'
                     + r'|\b(?:background|corner) (?:has|contains|shows) .{0,20}' + packaging, clause):
            incidental = True
            continue
        return 0
    return 1 if incidental else 2 if not re.search(packaging, description.lower()) else 3


def eligible(request):
    return (type(request.get('index')) is int and 1 <= request['index'] <= 3
        and request.get('has_product_selling_point') is False
        and request.get('display_goal') == 'general_product'
        and request.get('require_action') is False and request.get('require_result') is False
        and bool(request.get('checks')) and all(c.get('type') == 'display' for c in request['checks']))


def is_packaging_hook(request):
    if request.get('visual_purpose') != PURPOSE:
        return False
    if not eligible(request):
        raise ValueError('OPENING_HOOK_HAS_SELLING_POINT_OR_OUTSIDE_PREFIX')
    return True


def normalize(requests):
    result = deepcopy(requests)
    for row in result:
        if type(row.get('index')) is int and 1<=row['index']<=3:
            if type(row.get('has_product_selling_point')) is not bool:
                raise ValueError('OPENING_HOOK_CLASSIFICATION_REQUIRED')
            if row['has_product_selling_point'] is False and not eligible(row):
                raise ValueError('OPENING_HOOK_CLASSIFICATION_CONFLICT')
        if 'has_product_selling_point' in row and type(row['has_product_selling_point']) is not bool:
            raise ValueError('OPENING_HOOK_CLASSIFICATION_INVALID')
        if row.get('visual_purpose')==PURPOSE and not eligible(row):
            raise ValueError('OPENING_HOOK_HAS_SELLING_POINT_OR_OUTSIDE_PREFIX')
        if eligible(row):
            row['visual_purpose'] = PURPOSE
            row.setdefault('hook_original_checks', deepcopy(row['checks']))
            row['checks'] = [{'id': row['checks'][0]['id'], 'claim_text': row['text'], 'type': 'display',
                'question': '开头无本品具体卖点的钩子，用本品包装承接；包装身份与外观可辨，不要求泛红、竞品或功效实验。'}]
    return result


def validate_completed(requests):
    for row in requests:
        if row.get('visual_purpose') == PURPOSE:
            is_packaging_hook(row)
        if type(row.get('index')) is int and 1<=row['index']<=3:
            if type(row.get('has_product_selling_point')) is not bool:
                raise ValueError('OPENING_HOOK_CLASSIFICATION_REQUIRED')
            if row['has_product_selling_point'] is False and not is_packaging_hook(row):
                raise ValueError('OPENING_HOOK_NORMALIZATION_REQUIRED')


def assess_packaging(value, request, bounds):
    if not is_packaging_hook(request):
        raise ValueError('OPENING_HOOK_PURPOSE_REQUIRED')
    fact = value.get('hook_packaging')
    if (not isinstance(fact, dict)
            or any(type(fact.get(k)) is not bool for k in ('packaging_visible', 'packaging_primary', 'same_product'))
            or not isinstance(fact.get('identifiers_observed'), str) or not fact['identifiers_observed'].strip()
            or not isinstance(fact.get('evidence_intervals_us'), list)):
        raise ValueError('OPENING_HOOK_PACKAGING_FACTS_REQUIRED')
    spans = fact['evidence_intervals_us']
    for span in spans:
        if (not isinstance(span, dict) or type(span.get('start')) is not int or type(span.get('end')) is not int
                or not bounds['start'] <= span['start'] < span['end'] <= bounds['end']):
            raise ValueError('OPENING_HOOK_PACKAGING_OUTSIDE_WINDOW')
    minimum = min(600000, bounds['end'] - bounds['start'])
    ok = fact['packaging_visible'] and fact['packaging_primary'] and fact['same_product'] and any(s['end']-s['start'] >= minimum for s in spans)
    ids = [c['id'] for c in request['checks']]
    return {'status': 'HOOK_PACKAGING' if ok else 'NO_SELLING_POINT_INFORMATION',
        'selection_eligible': bool(ok), 'selection_policy': PURPOSE,
        'supported_ids': ids if ok else [], 'visual_requirement_supported_ids': ids if ok else [],
        'claim_truth_supported_ids': [], 'fully_supported_ids': [], 'uncovered_ids': ids,
        'uncovered_spoken_claims': [request['text']], 'all_spoken_claims_checked': False,
        'strength': 1 if ok else 0, 'evidence_intervals_us': spans if ok else [],
        'coverage_scope': 'packaging_accompaniment_only',
        'reasons': [] if ok else [{'reason': 'No confirmed readable same-product packaging interval'}]}
