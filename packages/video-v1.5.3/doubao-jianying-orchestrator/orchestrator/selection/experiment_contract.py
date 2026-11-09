"""Evidence and topic boundaries for the same hygiene experiment."""
from pathlib import Path
import json
import re
from .. import rulepack
from ..policy.continuity import adjacent


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
    if not adjacent(indices):
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


def adjacency_hint(first, second):
    """File order prioritizes retrieval only; it never proves experiment identity."""
    a,b=Path(first),Path(second)
    def sequence(path):
        match=re.search(r"(IMG|VID|DSC|DJI)[-_ ]?(\d+)$",path.stem,re.I)
        if match:return match.group(1).lower(),int(match.group(2))
        match=re.fullmatch(r"(.*?)(\d+)",path.stem)
        return (match.group(1).lower(),int(match.group(2))) if match else None
    x,y=sequence(a),sequence(b)
    comparable=a.parent==b.parent and x is not None and y is not None and x[0]==y[0]
    distance=y[1]-x[1] if comparable else None
    return {"adjacent":distance==1,"sequence_distance":distance,"acceptance_evidence":False}


def rank_pairs(first_sources, verification_sources):
    rows=[{"first":a,"verification":b,"hint":adjacency_hint(a,b)}
          for a in first_sources for b in verification_sources if Path(a)!=Path(b)]
    return sorted(rows,key=lambda r:(not r["hint"]["adjacent"],
        abs(r["hint"]["sequence_distance"]) if r["hint"]["sequence_distance"] is not None else float("inf"),
        str(r["first"]),str(r["verification"])))


def validate_pair(row, visible_ids):
    checks={"product","setup","material_state","verification_correspondence"}
    if not isinstance(row,dict) or row.get("verdict") not in {"matched","mismatch","uncertain"}:
        raise ValueError("EXPERIMENT_PAIR_VERDICT_INVALID")
    comparisons=row.get("comparisons")
    if not isinstance(comparisons,dict) or set(comparisons)!=checks:
        raise ValueError("EXPERIMENT_PAIR_COMPARISONS_REQUIRED")
    for value in comparisons.values():
        if (not isinstance(value,dict) or value.get("status") not in {"consistent","conflict","uncertain"}
                or not isinstance(value.get("observed"),str) or not value["observed"].strip()):
            raise ValueError("EXPERIMENT_PAIR_FACT_REQUIRED")
    phases=row.get("phases")
    if not isinstance(phases,list) or not isinstance(row.get("limitations"),str):
        raise ValueError("EXPERIMENT_PAIR_PHASES_REQUIRED")
    seen={}
    expected={"pour":"A","absorb":"A","press":"B","lift":"B","result":"B"}
    for phase in phases:
        if (not isinstance(phase,dict) or phase.get("kind") not in expected
                or phase["kind"] in seen or phase.get("part")!=expected[phase["kind"]]
                or not isinstance(phase.get("observed"),str) or not phase["observed"].strip()):
            raise ValueError("EXPERIMENT_PAIR_PHASE_INVALID")
        first,last=phase.get("first_frame"),phase.get("last_frame")
        if (type(first)is not int or type(last)is not int or first>last
                or first not in visible_ids[phase["part"]] or last not in visible_ids[phase["part"]]):
            raise ValueError("EXPERIMENT_PAIR_FRAME_NOT_SHOWN")
        seen[phase["kind"]]=phase
    if row["verdict"]=="matched":
        if set(seen)!=set(expected) or any(v["status"]!="consistent"for v in comparisons.values()):
            raise ValueError("EXPERIMENT_PAIR_MATCH_WITHOUT_EVIDENCE")
        if not (seen["pour"]["first_frame"]<=seen["absorb"]["first_frame"]
                and seen["press"]["last_frame"]<=seen["lift"]["first_frame"]
                and seen["lift"]["last_frame"]<=seen["result"]["first_frame"]):
            raise ValueError("EXPERIMENT_PAIR_PHASE_ORDER_INVALID")
    return row


def validate_pair_evidence(manifest, evidence, *, require_disjoint_windows=True):
    """One paired-source boundary for preview assembly and normal production."""
    from .. import ai_editor_contract as identity
    from ..media.temporal_samples import validate as validate_samples
    from ..vision.experiment_pair_review import input_binding, PROTOCOL
    if (evidence.get("task_id") != manifest["task_id"] or evidence.get("protocol") != PROTOCOL
            or evidence.get("rules") != identity.rules_identity(manifest)
            or evidence.get("context") != {"product": manifest["product"], "category": "weipin"}):
        raise ValueError("EXPERIMENT_PAIR_RULE_OR_CONTEXT_CHANGED")
    members = evidence.get("members")
    if not isinstance(members, list) or [m.get("part") for m in members] != ["A", "B"]:
        raise ValueError("EXPERIMENT_PAIR_MEMBERS_INVALID")
    allowed = {str(Path(p).resolve()) for p in manifest["material_sources"]}
    visible, bounds = {}, []
    for member in members:
        samples = validate_samples(member["samples"])
        if samples["original"]["path"] not in allowed:
            raise ValueError("EXPERIMENT_PAIR_OUTSIDE_TASK")
        a, b = member["window_frame_ids"]
        if type(a) is not int or type(b) is not int or not 0 <= a < b < len(samples["frames"]):
            raise ValueError("EXPERIMENT_PAIR_WINDOW_INVALID")
        ids = sorted({round(a + i * (b - a) / 7) for i in range(8)})
        if member["visible_frame_ids"] != ids:
            raise ValueError("EXPERIMENT_PAIR_SHOWN_FRAMES_CHANGED")
        visible[member["part"]] = set(ids)
        bounds.append((samples["frames"][a]["pts_us"],
                       min(samples["source_end_us"], samples["frames"][b]["pts_us"] + 16_667)))
    if identity.source_identity(evidence["sheet"]["path"]) != evidence["sheet"]:
        raise ValueError("EXPERIMENT_PAIR_SHEET_CHANGED")
    binding = evidence.get("input_binding") or {}
    bound_file = binding.get("file") or {}
    try:
        valid = (identity.source_identity(bound_file["path"]) == bound_file
                 and identity.file_sha256(bound_file["path"]) == binding["sha256"]
                 and json.loads(Path(bound_file["path"]).read_text("utf-8")) ==
                 input_binding(manifest, members, evidence["sheet"]["path"]))
    except (KeyError, OSError, ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("EXPERIMENT_PAIR_REVIEW_INPUT_CHANGED")
    row = validate_pair(evidence["observation"], visible)
    if row["verdict"] != "matched":
        raise ValueError("EXPERIMENT_PAIR_NOT_CONFIRMED")
    if (require_disjoint_windows and members[0]["samples"]["original"] == members[1]["samples"]["original"]
            and bounds[1][0] < bounds[0][1]):
        raise ValueError("EXPERIMENT_PAIR_SOURCE_ORDER_OR_REPLAY")
    return bounds
