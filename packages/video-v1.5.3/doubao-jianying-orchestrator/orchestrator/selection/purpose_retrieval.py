"""Validate text-only recall hints; none of these hints certify a selling point."""

MODES = {'direct_demonstration', 'feature_presentation', 'general_product', 'uncertain'}


def catalog(shots, scans):
    if len({s['shot_id'] for s in shots}) != len(shots):
        raise ValueError('PURPOSE_RECALL_DUPLICATE_SHOT')
    result = []
    for shot in shots:
        scan = scans.get(shot['shot_id'], {})
        text = scan.get('description')
        if scan.get('error') or not isinstance(text, str) or not text.strip():
            continue
        result.append({'shot_id': shot['shot_id'], 'description': text,
            'start_us': round(shot['source_start'] * 1e6),
            'end_us': round(shot['source_end'] * 1e6),
            'hints': [{k: h[k] for k in ('index', 'start_us', 'end_us')}
                for h in scan.get('hints', [])]})
    # Catalogue file order must not become a hidden preference for early IDs.
    return sorted(result, key=lambda s: (s['description'], s['start_us'], s['end_us'], s['shot_id']))


def _candidate(c, request, known, seen):
    if not isinstance(c, dict) or not isinstance(c.get('shot_id'), str):
        raise ValueError('PURPOSE_RECALL_CANDIDATE_INVALID')
    sid = c['shot_id']
    if sid not in known or sid in seen:
        raise ValueError('PURPOSE_RECALL_CANDIDATE_UNKNOWN_OR_DUPLICATE')
    claim = next((ch for ch in request['checks'] if ch['id'] == c.get('claim_id')), None)
    if (claim is None or c.get('claim_text') != claim.get('claim_text', request['text'])
            or c['claim_text'] not in request['text']):
        raise ValueError('PURPOSE_RECALL_ORIGINAL_CLAIM_REQUIRED')
    excerpt = c.get('description_excerpt')
    if (not isinstance(excerpt, str) or len(excerpt.strip()) < 4
            or excerpt not in known[sid]['description'] or c.get('mode') not in MODES
            or not isinstance(c.get('information_link'), str) or not c['information_link'].strip()):
        raise ValueError('PURPOSE_RECALL_DESCRIPTION_ANCHOR_REQUIRED')
    a, b = c.get('start_us'), c.get('end_us')
    if not request['require_action'] and not request['require_result'] and type(a) is int:
        # Fixed sentence arithmetic belongs to the program, not the LLM.
        b = a + request['segment_us']
    bounds = known[sid]
    if (type(a) is not int or type(b) is not int
            or not bounds['start_us'] <= a < b <= bounds['end_us']):
        raise ValueError('PURPOSE_RECALL_WINDOW_OUT_OF_RANGE')
    seen.add(sid)
    return {**c, 'end_us': b}


def validate(data, requests, entries, *, limit=6, rejected=None):
    rows = data.get('sentences')
    if (not isinstance(rows, list) or len(rows) != len(requests)
            or any(not isinstance(r, dict) or type(r.get('index')) is not int for r in rows)
            or {r['index'] for r in rows} != {r['index'] for r in requests}):
        raise ValueError('PURPOSE_RECALL_REQUEST_COVERAGE')
    known = {e['shot_id']: e for e in entries}
    by_index = {r['index']: r for r in requests}
    result = {}
    for row in rows:
        request = by_index[row['index']]
        candidates = row.get('candidates')
        if not isinstance(candidates, list) or len(candidates) > limit:
            raise ValueError('PURPOSE_RECALL_CANDIDATE_LIMIT')
        seen = set()
        normalized = []
        for c in candidates:
            try:
                normalized.append(_candidate(c, request, known, seen))
            except ValueError as error:
                if rejected is None:
                    raise
                rejected.append({'index':row['index'],'candidate':c,'reason':str(error)})
        result[row['index']] = normalized
    return result


def allocate(requests, rankings, entries, *, wave_size=3):
    """Diversify generic introductions without replacing a feature's own ranking."""
    from collections import Counter
    result = {}; reservations = Counter()
    general = lambda r: r.get('display_goal') == 'general_product' and not (r['require_action'] or r['require_result'])
    for request in sorted(requests, key=lambda r: (general(r), r['index'])):
        pool = list(rankings[request['index']])
        if general(request):
            known = {c['shot_id'] for c in pool}
            for e in entries:
                if e['shot_id'] not in known and e['end_us']-e['start_us'] >= request['segment_us']:
                    check = request['checks'][0]
                    pool.append({'shot_id':e['shot_id'],'claim_id':check['id'],
                        'claim_text':check.get('claim_text',request['text']), 'mode':'uncertain',
                        'description_excerpt':e['description'],
                        'information_link':'通用介绍同商品补充召回；必须实际复核，不是卖点证据',
                        'start_us':e['start_us'],'end_us':e['start_us']+request['segment_us'],
                        'generic_reservoir':True})
            pool = [c for _,c in sorted(enumerate(pool),key=lambda pair:(reservations[pair[1]['shot_id']],pair[0]))]
        result[request['index']] = pool[:wave_size*2]
        reservations.update(c['shot_id'] for c in pool[:wave_size])
    return result


def windows(candidates, shots):
    known = {s['shot_id']: s for s in shots}
    return [{**known[c['shot_id']], 'source_start': c['start_us'] / 1e6,
        'source_end': c['end_us'] / 1e6, 'retrieval_hint': c} for c in candidates]
