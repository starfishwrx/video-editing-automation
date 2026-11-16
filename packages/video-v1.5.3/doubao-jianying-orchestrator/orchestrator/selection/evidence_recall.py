"""Recall a reviewed source window, then let the normal placement and QC gates run."""
from __future__ import annotations

import re

from .. import ai_editor_contract as contract
from ..vision import evidence_store as store


def _terms(text):
    # Retrieval only. Overlap cannot establish a new visual claim.
    words = re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9]+", text.casefold())
    return {s[i:i+2] for s in words for i in range(max(1, len(s)-1))}


def _canonical(text):
    return re.sub(r"[\s，。！？、,.!?]+", "", str(text)).casefold()


def _equivalent_claim(text):
    # These wording variants preserve the action, object, result and sequence.
    # Unrecognised words and all numeric/environmental qualifiers stay in the key.
    phrase = _canonical(text)
    for before, after in (("纸巾表面", "纸面"), ("重新摊开", "重新展开"),
                          ("不会掉絮", "不掉絮"), ("吸收水分", "吸水"),
                          ("反复揉捏", "反复揉搓"), ("仍然完整", "依然完整")):
        phrase = phrase.replace(before, after)
    return phrase


def _gap(text, facts):
    # Conditions which commonly change the meaning of a demonstration.
    conditions = (("湿水", r"湿水|沾水|浸湿|打湿|遇水|浸水|湿润"),
                  ("揉搓", r"揉搓|揉过|揉完|揉捏"), ("展开", r"展开|摊开"),
                  ("擦脸", r"擦脸|擦拭脸部"), ("计时", r"秒|分钟"))
    return [name for name, pattern in conditions if re.search(pattern, text)
            and not re.search(pattern, facts)]


def _paper_equivalent(text, observed):
    # Closed grammar for a visible sequence. Additional adjectives, quantities,
    # wet conditions and negation cannot pass via a substring match.
    phrase = _canonical(text)
    if not re.fullmatch(r"(?:纸巾|纸张|纸面)?(?:揉过|揉完|揉搓后|揉搓过后|揉过后)"
                        r"(?:再|重新)?(?:展开|摊开)(?:后)?(?:纸面|纸巾|纸张|表面)"
                        r"(?:还是|仍然|仍|依然)?(?:完整|完好)(?:的)?", phrase):
        return False
    # A positive ordered observation, with the same physical object, is required.
    # Reject competing phases or uncertainty instead of token-matching them.
    return bool(re.fullmatch(
        r"(?:双手|手)(?:拿取|拿起|拿着)?(?:一张)?(?:白色)?纸巾(?:进行|反复|用力|进行反复)?"
        r"揉搓(?:、挤压)?(?:动作)?[，,]随后(?:将)?纸巾(?:重新)?展开(?:平铺)?(?:展示)?[，,]"
        r"纸巾表面完整(?:[，,]未见明显毛絮(?:或破损)?)?[。.]?", observed))



def _mapped_claims(answer, facts, points):
    """Admit a text-only paraphrase only when every point maps to a verified old anchor."""
    if not isinstance(answer, dict) or answer.get("entire_sentence_supported") is not True or answer.get("missing") != []:
        return {}
    old = {p["id"]: p for p in facts["claims"]}
    proposed = answer.get("points")
    if not isinstance(proposed, list) or len(proposed) != len(points):
        return {}
    requested = {p["id"] for p in points}
    mapped = {}
    for row in proposed:
        if not isinstance(row, dict):
            return {}
        new, prior = row.get("id"), row.get("prior_point")
        if isinstance(prior, str) and prior not in old:
            # Model responses sometimes return the old label verbatim. Resolve it
            # only when exactly one stored ID has that complete label.
            aliases = [key for key, point in old.items() if point.get("text") == prior]
            prior = aliases[0] if len(aliases) == 1 else None
        if (not isinstance(new, str) or new not in requested or new in mapped
                or not isinstance(prior, str) or prior not in old
                or row.get("relation") != "same_meaning" or row.get("new_condition") is not False
                or row.get("basis") != facts["basis"] or row.get("quote") != facts["observation"]):
            return {}
        mapped[new] = prior
    return mapped


def _eligible(manifest, sentence):
    return (store.database() and sentence.get("selling_points")
            and not any(sentence.get(k) for k in ("qc_feedback","hook_neighbor_context",
                                                    "action_continuation","visual_requirements"))
            and not any(manifest.get(k) for k in ("revision_feedback","shot_notes_general",
                                                   "shot_notes","material_index_sha256")))


def inspect(manifest, sentence, shot, *, rows=None, judge=None):
    if not _eligible(manifest, sentence):
        return None, None
    text = sentence["text"]
    best, score = None, 0
    semantic = None
    candidates = list(store.windows(manifest, shot) if rows is None else rows)
    negatives = [(r["start_us"], r["end_us"]) for r in candidates
                 if r["grade"] == "review" and r["facts"].get("completed_dense_review")
                 and r["facts"].get("verdict") == "mismatch"
                 and _canonical(r["facts"].get("claim_context")) == _canonical(text)
                 and store.proof_available(r)]
    for row in candidates:
        f = row["facts"]
        observed = str(f.get("observation") or "")
        words = " ".join([observed, str(f.get("action") or ""), str(f.get("result") or ""),
                          " ".join(f.get("text") or []), *(p["text"] for p in f.get("claims") or [])])
        overlap = len(_terms(text) & _terms(words))
        missing = _gap(text, observed)
        if overlap > score:
            best, score = (row, missing), overlap
        if (row["grade"] not in ("qc","review") or not observed or f.get("verdict") != "matched"
                or f.get("same_product") is not True or missing
                or not set(sentence.get("scene_ids") or []) <= set(f.get("scene_ids") or [])):
            continue
        if any(a < row["end_us"] and b > row["start_us"] for a, b in negatives):
            continue
        if not f.get("evidence_intervals_us") or f.get("basis") not in ("visible_result","readable_label"):
            continue
        if row["grade"] == "review" and not f.get("completed_dense_review"):
            continue
        if not isinstance(f.get("confidence"), (int, float)) or f["confidence"] < .6:
            continue
        if not store.proof_available(row):
            continue
        exact = _equivalent_claim(text) == _equivalent_claim(f.get("claim_context"))
        equivalent = (row["grade"] == "qc" and f.get("basis") == "visible_result"
                      and _paper_equivalent(text, observed))
        claims = {_equivalent_claim(p["text"]): p["id"] for p in f.get("claims") or []}
        points = sentence["selling_points"]
        direct = (exact or equivalent) and all(_equivalent_claim(p["text"]) in claims or
                  (equivalent and _paper_equivalent(p["text"], observed)) for p in points)
        mapped = {p["id"]: claims.get(_equivalent_claim(p["text"])) for p in points}
        if not direct:
            if not judge or overlap < 2 or not f.get("claims"):
                continue
            if semantic is None or overlap > semantic[0]:
                semantic = (overlap, row)
            continue
        return_value = _review_result(row, points, mapped, shot)
        if return_value is not None:
            return return_value, None
    if semantic:
        row = semantic[1]
        f = row["facts"]
        answer = judge(row, f["observation"], sentence)
        mapped = _mapped_claims(answer, f, sentence["selling_points"])
        if mapped:
            result = _review_result(row, sentence["selling_points"], mapped, shot)
            if result:
                return result, None
        gaps = answer.get("missing") if isinstance(answer, dict) else None
        if isinstance(gaps, list) and gaps and all(isinstance(g, str) for g in gaps):
            best = (row, gaps)
    return None, best


def _review_result(row, points, mapped, shot):
    f = row["facts"]
    observed = f["observation"]
    window = {"start":row["start_us"], "end":row["end_us"]}
    checks = [dict(check, id=p["id"]) for p in points for check in f.get("claim_checks") or []
              if mapped.get(p["id"]) == check.get("id")]
    if row["grade"] == "review" and (not checks or len(checks) != len(points)):
        return None
    proof = {k:f[k] for k in ("basis","observation","scene_ids","time_sensitive","evidence_intervals_us",
                                "presentation_observation","playback_observation","presentation_status",
                                "preferred_speed","speed_range_override")
             if k in f}
    return {**proof, "verdict":"matched", "same_product":True,
            "reason":observed, "confidence":f["confidence"],
            "claim_checks":checks, "supported_point_ids":[p["id"] for p in points],
            "review_window_us":window, "source_identity":contract.source_identity(shot["video"]),
            "shot_id":shot["shot_id"], "frame_times_us":f.get("frame_times_us", []),
            "sheet":f.get("sheet", ""), "dense_required":False,
            "evidence_id":row["id"], "evidence_grade":row["grade"]}


def catalog(manifest, shots):
    if not store.database() or manifest.get("material_index_sha256"):
        return {}
    found = {}
    for shot in shots:
        rows = store.windows(manifest, shot)
        facts = []
        for row in rows:
            f = row["facts"]
            note = (f.get("observation") or " ".join(f.get("text") or [])
                    or "，".join(str(f[k]) for k in ("object","action","result") if f.get(k)))
            if note and len(facts) < 6:
                facts.append({"window":[row["start_us"]/1e6, row["end_us"]/1e6],
                              "observation":note, "basis":f.get("basis", row["grade"])})
        if facts:
            found[shot["shot_id"]] = facts
    return found
