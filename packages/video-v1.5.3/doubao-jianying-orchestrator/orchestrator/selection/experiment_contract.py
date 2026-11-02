"""Evidence and topic boundaries for a single-source hygiene experiment."""
from .. import rulepack


def contract_for(manifest):
    context = rulepack.editorial_context(manifest)
    if context["category"] != "weipin":
        raise ValueError("EXPERIMENT_CATEGORY_UNSUPPORTED")
    return context["rules"]["experiment_contracts"]["absorb_rewet"]


def validate_observation(row, frames, required):
    if not isinstance(row, dict) or row.get("verdict") not in {"complete", "incomplete", "uncertain"}:
        raise ValueError("EXPERIMENT_VERDICT_INVALID")
    for key in ("same_product", "continuous_specimen", "no_splice"):
        if type(row.get(key)) is not bool:
            raise ValueError("EXPERIMENT_FACT_FLAG_MISSING")
    if not isinstance(row.get("observed"), str) or not row["observed"].strip():
        raise ValueError("EXPERIMENT_FACT_REQUIRED")
    phases = row.get("phases")
    if not isinstance(phases, list):
        raise ValueError("EXPERIMENT_PHASES_REQUIRED")
    seen = {}
    for phase in phases:
        if (not isinstance(phase, dict) or phase.get("kind") not in required
                or phase["kind"] in seen or not isinstance(phase.get("observed"), str)
                or not phase["observed"].strip()):
            raise ValueError("EXPERIMENT_PHASE_INVALID")
        first, last = phase.get("first_frame"), phase.get("last_frame")
        if (type(first) is not int or type(last) is not int
                or not 0 <= first <= last < len(frames)):
            raise ValueError("EXPERIMENT_FRAME_ID_OUTSIDE_INPUT")
        seen[phase["kind"]] = phase
    if row["verdict"] == "complete":
        if set(seen) != set(required) or not all(row[k] for k in ("same_product", "continuous_specimen", "no_splice")):
            raise ValueError("EXPERIMENT_COMPLETE_WITHOUT_EVIDENCE")
        # Absorption may occur during pouring. Pressing must follow the pour;
        # the tissue result must follow lifting rather than a prepared pose.
        if not (seen["pour"]["first_frame"] <= seen["absorb"]["first_frame"]
                <= seen["press"]["first_frame"]
                and seen["pour"]["last_frame"] <= seen["press"]["first_frame"]
                and seen["absorb"]["last_frame"] <= seen["press"]["first_frame"]
                and seen["press"]["last_frame"] <= seen["lift"]["first_frame"]
                <= seen["lift"]["last_frame"]
                <= seen["result"]["first_frame"]):
            raise ValueError("EXPERIMENT_PHASE_ORDER_INVALID")
    return row


def validate_roles(timeline, rows, rules):
    known = set(rules["allowed_roles"]) | set(rules["forbidden_roles"])
    if (not isinstance(rows, list) or len(rows) != len(timeline.sentences)
            or any(not isinstance(r, dict) for r in rows)
            or [(r.get("sentence_id"), r.get("text")) for r in rows]
            != [(s.sentence_id, s.text) for s in timeline.sentences]
            or any(r.get("role") not in known for r in rows)):
        raise ValueError("EXPERIMENT_SENTENCE_ROLE_IDENTITY")
    return rows


def validate_group(timeline, sentence_ids, roles, rules):
    validate_roles(timeline, roles, rules)
    ids = tuple(sentence_ids)
    selected = [r for r in roles if r["sentence_id"] in ids]
    if not ids or tuple(r["sentence_id"] for r in selected) != ids:
        raise ValueError("EXPERIMENT_GROUP_IDENTITY")
    indices = [i for i,r in enumerate(roles) if r["sentence_id"] in ids]
    if indices != list(range(indices[0],indices[-1]+1)):
        raise ValueError("EXPERIMENT_GROUP_NOT_ADJACENT")
    if any(r["role"] not in rules["allowed_roles"] for r in selected):
        raise ValueError("EXPERIMENT_GROUP_TOPIC_CONFLICT")
    if not any(r["role"] in {"absorption", "rewet"} for r in selected):
        raise ValueError("EXPERIMENT_GROUP_HAS_NO_FUNCTION")


def candidate_groups(timeline, roles, rules):
    validate_roles(timeline, roles, rules)
    result, current = [], []
    for row in roles + [{"role": "other"}]:
        if row["role"] in rules["allowed_roles"]:
            current.append(row)
        else:
            if any(r["role"] in {"absorption", "rewet"} for r in current):
                result.append(tuple(r["sentence_id"] for r in current))
            current = []
    return result
