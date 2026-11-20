"""One contract for independent visual responses and their delivery consumers."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
from pathlib import Path
from typing import Any

REPORT_VERSION = "chatcut-visual-qc-v3"
RESPONSE_VERSION = "chatcut-visual-qc-response-v3"
AI_REPORT_VERSION = "chatcut-visual-qc-v7"
AI_RESPONSE_VERSION = "chatcut-visual-qc-response-v7"
MATCH_LEVELS = {"DIRECT", "ACCEPTABLE_DEGRADED", "MISMATCH", "INSUFFICIENT_EVIDENCE"}
OVERALL_LEVELS = {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS", "FAIL", "UNCERTIFIED"}


def _draft_documents():
    path = Path(__file__).with_name('runtime') / 'draft_documents.py'
    spec = importlib.util.spec_from_file_location('_qc_draft_documents', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ai_contract():
    if __package__:
        from . import ai_editor_contract as contract
    else:
        spec = importlib.util.spec_from_file_location("_chatcut_ai_contract", Path(__file__).with_name("ai_editor_contract.py"))
        contract = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contract)
    return contract


def _current_editorial_rules(report: dict) -> bool:
    contract=_ai_contract()
    recorded = report.get("editorial_rules")
    return (isinstance(recorded, dict) and report.get("policy_version") == contract.POLICY_VERSION
        and {k: recorded.get(k) for k in ("category", "versions")} == contract.rules_identity({"category": recorded.get("category")}))


def _source_identity(path: str) -> dict:
    source = Path(path).resolve(strict=True)
    info = source.stat()
    return {"path": str(source), "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _source_bindings_valid(report: dict, draft: Path | None = None) -> bool:
    bindings = report.get("source_bindings")
    if (not isinstance(bindings, list) or not bindings
            or any(not isinstance(row, dict) or not isinstance(row.get("path"), str)
                   or not isinstance(row.get("identity"), dict) for row in bindings)):
        return False
    if any(not isinstance(row["identity"].get("path"), str)
           or type(row["identity"].get("size")) is not int or row["identity"]["size"] < 0
           or type(row["identity"].get("mtime_ns")) is not int for row in bindings):
        return False
    if draft is not None:
        paths = [os.path.normcase(str(Path(row["path"]).resolve(strict=True))) for row in bindings]
        if any(_source_identity(row["path"]) != row["identity"] for row in bindings):
            return False
        # Every source actually referenced by the video timeline needs a binding.
        info = _draft_documents().timeline(draft)
        used = {str(segment.get("material_id") or "") for track in info.get("tracks", [])
                for segment in track.get("segments", [])}
        for material in (info.get("materials") or {}).get("videos", []):
            if str(material.get("id") or material.get("material_id") or "") not in used:
                continue
            raw = str(material.get("path") or material.get("media_path") or "")
            source = Path(raw)
            try:
                readable = source.is_file()
            except OSError:
                readable = False
            if not readable:
                source = draft / "media" / Path(raw).name
            if os.path.normcase(str(source.resolve(strict=True))) not in paths:
                return False
    return True


def draft_metadata_fingerprint(draft: Path) -> str:
    """Hash JianYing timeline metadata only, never the referenced media tree."""
    root = Path(draft).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("DRAFT_DIRECTORY_REQUIRED")
    rows = []
    names = ['draft_info.json', 'draft_content.json', 'ai_selection_identity.json', 'prepared_sources.json']
    if _draft_documents().is_native(root):
        names += ['headless-production.json', 'draft_meta_info.json', 'template-2.tmp',
                  'draft_content.json.bak', 'timeline_layout.json', 'Timelines/project.json', 'Timelines/project.json.bak',
                  'caption_identity.json', 'audio_identity.json']
        names += [str(p.relative_to(root)) for p in (root / 'Timelines').glob('*/draft_content.json')]
        names += [str(p.relative_to(root)) for p in (root / 'Timelines').glob('*/draft_content.json.bak')]
        names += [str(p.relative_to(root)) for p in (root / 'Timelines').glob('*/template-2.tmp')]
    for name in names:
        path = root / name
        if path.is_symlink():
            raise ValueError("DRAFT_METADATA_SYMLINK_REJECTED")
        if path.is_file():
            rows.append({"name": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not rows:
        raise ValueError("DRAFT_METADATA_MISSING")
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def normalize_match_level(row: dict[str, Any]) -> str:
    raw = str(row.get("match_level") or row.get("level") or "").strip().upper()
    # Old negative judgments still block; a boolean success never proves DIRECT.
    level = {"FAIL": "MISMATCH", "UNCERTAIN": "INSUFFICIENT_EVIDENCE",
             "NOT_VERIFIED": "INSUFFICIENT_EVIDENCE",
             "DEGRADED": "ACCEPTABLE_DEGRADED"}.get(raw, raw)
    if level == "MISMATCH":
        return level
    if not isinstance(level, str) or level not in MATCH_LEVELS:
        return "INSUFFICIENT_EVIDENCE"
    expected = level in {"DIRECT", "ACCEPTABLE_DEGRADED"}
    if row.get("visual_ok") is not expected:
        return "INSUFFICIENT_EVIDENCE"
    return level


def presentation_check_errors(row: dict, required_actors=None) -> list[str]:
    required = row.get('presentation_expected_actors',[]) if required_actors is None else required_actors
    if (not isinstance(required,(list,tuple,set)) or any(not isinstance(a,str) or a not in {'product','occluder'} for a in required)):
        return ['PRESENTATION_EXPECTATION_INVALID']
    check=row.get('action_check')
    if not isinstance(check,dict):
        return ['PRESENTATION_ACTION_UNVERIFIED']
    actors={'product':{'product'},'occluder':{'occluder'},'product_and_occluder':{'product','occluder'}}
    actor=check.get('actor')
    actual=actors.get(actor,set()) if isinstance(actor,str) else set()
    if (not actual or not set(required) <= actual or check.get('change_visible') is not True
            or check.get('result_visibility') != 'clear' or not isinstance(check.get('observation'),str)
            or not check['observation'].strip()):
        return ['PRESENTATION_ACTION_UNVERIFIED']
    return []


def bound_selection(report):
    segments=report.get('selection_segments')
    if not isinstance(segments,list) or not segments or any(not isinstance(s,dict) for s in segments):
        raise ValueError('QC_SELECTION_BINDING_MISSING')
    if _ai_contract().digest(segments) != report.get('selection_sha256'):
        raise ValueError('QC_SELECTION_BINDING_CHANGED')
    return segments


EXPERIMENT_OUTCOME_PROTOCOL = "cut-outcomes-v2"
OUTCOME_SUBJECT = {"absorb": "pad_liquid", "result": "lifted_tissue"}
EXPERIMENT_OUTCOMES = {
    "absorb": ("receding", "unchanged", "uncertain"),
    "result": ("clean_tissue", "stained_tissue", "uncertain"),
}


def valid_experiment_observations(events):
    phases = ("pour", "absorb", "press", "lift", "result")
    return isinstance(events, list) and all(
        isinstance(entry, dict) and entry.get("event") in phases and isinstance(entry.get("frames"), list)
        and bool(entry["frames"]) and all(type(n) is int and n > 0 for n in entry["frames"])
        and entry["frames"] == sorted(set(entry["frames"]))
        and (entry["event"] not in EXPERIMENT_OUTCOMES
             or entry.get("outcome") in EXPERIMENT_OUTCOMES[entry["event"]])
        and (entry["event"] != "result" or entry.get("expected") in ("clean_tissue", "stained_tissue", "inspect"))
        for entry in events)


def _stained_result_supported(members):
    """A visible stain needs support in the group's frozen narration, even for an inspect verdict."""
    texts = [str(member.get("text") or "") for member in members]
    neutral = re.compile(r"(?:会不会|是否|有没有|有无|看看.{0,6}有没有)(?:发生|出现|有)?(?:反渗|回渗|回流|湿痕|水渍|沾液|染色|变湿)")
    explicit_stain = re.compile(r"(?:纸巾|纸面|白纸)(?:上|确实|明显|已经|有|出现|了|可见|残留|沾有)*(?:湿痕|水渍|蓝色液体|被染湿|变湿)")
    dry = re.compile(r"(?:不|无|没)(?:会|容易|易|有|再|出|任何|一点儿)*(?:反渗|回渗|回流|沾液|粘腻|黏腻)|干干?爽爽?|干燥|干净|洁白")
    return (any(neutral.search(text) or explicit_stain.search(text) for text in texts)
            and not any(dry.search(neutral.sub("", text)) for text in texts))


def _opposes_outcome(fact, outcome):
    # Only an explicit observation about this object can contradict the typed result.
    if outcome == "receding":
        # A description may establish the liquid as subject in one clause and
        # state the comparison in the next. Preserve that scope up to a new subject.
        for sentence in re.split(r"[。！？;；\n]", fact):
            subject = None
            for clause in re.split(r"[,，]", sentence):
                if re.search(r"液体|液面|积液|水渍|水量|水迹|水位", clause):
                    subject = "liquid"
                elif re.search(r"纸巾|纸面|白纸|包装|手指", clause):
                    subject = "other"
                if subject == "liquid" and re.search(
                        r"(?:没有|未见|并未|未能|看不出|无法确认|看不到)(?:发生|明显|显著|明显的|任何)*"
                        r"(?:减少|变少|消退|变化|区别|下降|被吸收)", clause):
                    return True
        return bool(re.search(r"(?:液面|液体|积液|水渍)(?:前后|仍然|仍|始终|持续|表面)*(?:保持原状|没有变化|未见变化|(?:没有|未见|未能|不见)(?:发生|明显|显著)*(?:区别|变化|减少|变少|消退|吸收))"
                              r"|未见(?:液体)?明显(?:扩散或消失|减少|吸收)", fact))
    if outcome == "clean_tissue":
        return bool(re.search(r"(?:纸巾|纸面|白纸)(?:上|表面|本身|明显|清楚|可见|仍|有|着|呈现|出现|沾有|沾染|残留|留有|的|"
                              r"少量|少许|一点点?|一小(?:片|块)|一片|几滴|些许|浅|淡)*"
                              r"(?:(?:蓝|红|黄|绿|紫|褐|有|无)色(?:的)?(?:液体|湿痕|水迹|液渍)|液体|水渍|水迹|湿痕|液渍|湿了|湿润|变湿|沾湿)", fact))
    if outcome == "stained_tissue":
        return bool(re.search(r"(?:纸巾|纸面|白纸)(?:上|表面|本身|仍然|保持|看起来|仍|很|是|完全)*"
                              r"(?:干燥|干净|未见湿痕|没有沾液)", fact))
    return False


def experiment_outcomes_disputed(group, rows):
    """Uncertain or conflicting readings require another look at the same draft."""
    for member in group["input"]["members"]:
        row = rows.get(member["index"]) or {}
        for event in row.get("visible_experiment_events") or []:
            if not isinstance(event, dict) or event.get("event") not in EXPERIMENT_OUTCOMES:
                continue
            outcome = event.get("outcome")
            facts = [f for f in row.get("physical_outcomes") or [] if isinstance(f, dict)
                     and f.get("event") == event["event"] and set(f.get("frames") or []).intersection(event.get("frames") or [])]
            if (outcome == "uncertain" or not facts or _opposes_outcome(str(row.get("observed") or ""), outcome)
                    or any(f.get("outcome") != outcome or _opposes_outcome(str(f.get("observation") or ""), outcome)
                           for f in facts)):
                return True
    return False


def group_events_visible(group, model_rows):
    """Check the independently reported events against the actual per-sentence cuts."""
    observed = []
    tissue = _ai_contract().continuity.tissue_event_observed
    contact = lift = False
    witnessed = {"pour": set(), "absorb": set(), "result": set()}
    members = group["input"]["members"]
    for member in members:
        row = model_rows.get(member["index"]) or {}
        entries = row.get("visible_experiment_events")
        supplied = row.get("experiment_output_frames")
        if row.get("visual_ok") is not True or not valid_experiment_observations(entries) or not isinstance(supplied, list):
            return False
        physical = [p.get("experiment_phase") for p in member["parts"]]
        seen = [entry["event"] for entry in entries]
        for entry in entries:
            event = entry["event"]
            if event == "absorb" and entry["outcome"] != "receding":
                return False
            if event == "result" and (entry["outcome"] == "uncertain"
                    or entry["expected"] not in ("inspect", entry["outcome"])
                    or (entry["outcome"] == "stained_tissue" and not _stained_result_supported(members))):
                return False
            if event in EXPERIMENT_OUTCOMES and _opposes_outcome(str(row.get("observed") or ""), entry["outcome"]):
                return False
            for number in entry["frames"]:
                sample = supplied[number - 1] if number <= len(supplied) else None
                if (not isinstance(sample, list) or len(sample) != 2 or any(type(n) is not int for n in sample)
                        or not 1 <= sample[0] <= len(physical) or physical[sample[0] - 1] != event):
                    return False
                if event in EXPERIMENT_OUTCOMES:
                    facts = [fact for fact in row.get("physical_outcomes", []) if isinstance(fact, dict)
                             and fact.get("part") == sample[0] and fact.get("event") == event]
                    if (len(facts) != 1 or facts[0].get("subject") != OUTCOME_SUBJECT[event]
                            or facts[0].get("outcome") != entry["outcome"]
                            or not isinstance(facts[0].get("observation"), str) or not facts[0]["observation"].strip()
                            or _opposes_outcome(facts[0]["observation"], entry["outcome"])):
                        return False
                    cited = facts[0].get("frames")
                    if (not isinstance(cited, list) or not cited or any(type(n) is not int or not 1 <= n <= len(supplied)
                            or not isinstance(supplied[n - 1], list) or len(supplied[n - 1]) != 2
                            or supplied[n - 1][0] != sample[0] for n in cited)):
                        return False
                    if number not in cited:
                        continue
                if event in witnessed:
                    witnessed[event].add(sample[1])
            if not observed or event != observed[-1]:
                observed.append(event)
        fact = row.get("observed")
        if not isinstance(fact, str) or not fact.strip():
            return False
        contact = contact or ("press" in seen and tissue(fact, "press"))
        lift = lift or ("lift" in seen and tissue(fact, "lift"))
    return (contact and lift and observed == ["pour", "absorb", "press", "lift", "result"]
            and all(len(ticks) >= 2 for ticks in witnessed.values())
            and max(witnessed["result"]) - min(witnessed["result"]) + 1
                >= _ai_contract().continuity.experiment_minimum_frames("result", group["input"].get("fps", 30)))


def experiment_selection_errors(segments, rows):
    by_index = {row["index"]: row for row in rows}
    for index, item in enumerate(segments, 1):
        group = item.get("continuity_group") or {}
        if group.get("schema") != "reviewed-experiment-sentence-group-v1":
            continue
        members = group["input"]["members"]
        if index == members[0]["index"]:
            if any(by_index.get(m["index"], {}).get("physical_outcome_protocol") != EXPERIMENT_OUTCOME_PROTOCOL
                   for m in members):
                return ["EXPERIMENT_OUTCOME_REVIEW_REQUIRED"]
            if not group_events_visible(group, by_index):
                return ["EXPERIMENT_OUTPUT_STAGES_UNVERIFIED"]
    return []


def presentation_expected_actors(phases):
    return sorted({p['change']['actor'] for p in phases
                   if (p.get('change') or {}).get('actor') in {'product','occluder'}})


def promotion_group_requires_stages(item):
    members = ((item.get("continuity_group") or {}).get("input") or {}).get("members") or []
    return (len(members) > 1 and _ai_contract().presentation.promotion_motion(item.get("text")))


def member_stage_observed(row):
    stage = row.get("member_stage")
    return (isinstance(stage, dict)
            and stage.get("phase") in ("change", "first_readable", "hold", "camera", "uncertain")
            and isinstance(stage.get("observation"), str) and bool(stage["observation"].strip()))


def member_repair(row):
    if member_stage_observed(row) and row["member_stage"]["phase"] in {"hold", "camera"}:
        return "replace"
    return "review"


def carton_landing_required(item):
    """Identify a single carton reveal or generic parcel presentation by its selected purpose."""
    if item.get('selection_mode') != 'ai_editor' or item.get('multi_shot') or item.get('continuity_group'):
        return False
    receipt = item.get('ai_selection') or {}
    phase = receipt.get('presentation_observation') or {}
    action = ' '.join(str(value or '') for value in (
        phase.get('action'), phase.get('result'), receipt.get('observation')))
    carton = re.search(r'开箱|纸箱|箱盖|箱内|外箱|快递箱|包装箱|箱子', action)
    opening = re.search(r"(?:开箱|掀盖|开盖)|(?:打开|掀开|翻开|拉开|揭开|掀起).{0,50}(?:纸箱|箱盖|盖板|封盖|箱子)|"
                        r"(?:箱盖|盖板|封盖).{0,18}(?:掀|翻开|打开|移开)", action)
    return bool(carton and (opening or _ai_contract().presentation.promotion_motion(item.get('text')) or receipt.get('sentence_kind') in {'hook', 'product_intro'}
        or (phase.get('change') or {}).get('kind') == 'reveal'))


def clear_carton_landing(check):
    return (isinstance(check, dict) and check.get('view') in {'retail_product', 'contents_exposed'}
        and check.get('flap_over_goods') == 'no'
        and isinstance(check.get('observation'), str) and bool(check['observation'].strip()))


def presentation_selection_errors(segments,rows):
    """Derive required action checks from the bound selection, never row flags."""
    contract=_ai_contract()
    by_index={r.get('index'):r for r in rows if isinstance(r,dict)}
    expected,groups={},{}
    for index,item in enumerate(segments,1):
        if item.get('selection_mode') != 'ai_editor':
            continue
        parts=item.get('span_parts') if item.get('multi_shot') else [item]
        phases=[(p.get('ai_selection') or {}).get('presentation_observation') for p in parts or []]
        group=item.get('continuity_group') or {}
        group_phases=(group.get('input') or {}).get('presentation_observations',[])
        phases=[contract.presentation.get(p) for p in phases+group_phases if p is not None]
        if not phases and not contract.presentation.promotion_motion(item.get("text")):
            continue
        actors=presentation_expected_actors(phases)
        expected[index]=actors
        if group_phases:
            groups.setdefault(contract.digest(group['input']),[]).append(index)
    errors={}
    for index,item in enumerate(segments,1):
        if promotion_group_requires_stages(item):
            stage = (by_index.get(index) or {}).get("member_stage") or {}
            if not member_stage_observed(by_index.get(index) or {}) or stage["phase"] not in {"change", "first_readable"}:
                errors.setdefault(index, []).append("PROMOTION_MEMBER_ACTION_MISSING")
        if carton_landing_required(item) and not clear_carton_landing((by_index.get(index) or {}).get('terminal_check')):
            errors.setdefault(index,[]).append('CARTON_LANDING_UNVERIFIED')
    for index,actors in expected.items():
        row=by_index.get(index) or {}
        if row.get('presentation_check_required') is not True or row.get('presentation_expected_actors') != actors:
            errors.setdefault(index,[]).append('PRESENTATION_EXPECTATION_CHANGED')
        errors.setdefault(index,[]).extend(presentation_check_errors(row,actors))
    for indices in groups.values():
        signatures=[]
        for index in indices:
            check=(by_index.get(index) or {}).get('action_check')
            if isinstance(check,dict):
                signatures.append(json.dumps([check.get(k) for k in ('actor','change_visible','result_visibility')],sort_keys=True))
        if len(signatures)!=len(indices) or len(set(signatures))!=1:
            for index in indices:
                errors.setdefault(index,[]).append('PRESENTATION_GROUP_CHECK_INCONSISTENT')
    return {i:sorted(set(codes)) for i,codes in errors.items() if codes}


def row_errors(row: Any) -> list[str]:
    if not isinstance(row, dict):
        return ["SEGMENT_NOT_OBJECT"]
    errors = []
    if type(row.get("index")) is not int or row["index"] < 1:
        errors.append("INDEX_INVALID")
    level = row.get("match_level")
    if not isinstance(level, str) or level not in MATCH_LEVELS:
        errors.append("MATCH_LEVEL_INVALID")
    if type(row.get("visual_ok")) is not bool:
        errors.append("VISUAL_OK_INVALID")
    elif isinstance(level, str) and level in MATCH_LEVELS and row["visual_ok"] != (level in {"DIRECT", "ACCEPTABLE_DEGRADED"}):
        errors.append("VERDICT_CONFLICT")
    for key in ("observed", "reason"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            errors.append(key.upper() + "_MISSING")
    confidence = row.get("confidence")
    try:
        confidence_valid = type(confidence) in (int, float) and math.isfinite(confidence) and 0 <= confidence <= 1
    except OverflowError:
        confidence_valid = False
    if not confidence_valid:
        errors.append("CONFIDENCE_INVALID")
    elif confidence == 0 and isinstance(level, str) and level in {"DIRECT", "ACCEPTABLE_DEGRADED"}:
        errors.append("POSITIVE_VERDICT_ZERO_CONFIDENCE")
    if 'presentation_check_required' in row and type(row['presentation_check_required']) is not bool:
        errors.append('PRESENTATION_MARKER_INVALID')
    if row.get('presentation_check_required') and isinstance(level,str) and level in {'DIRECT','ACCEPTABLE_DEGRADED'}:
        errors.extend(presentation_check_errors(row))
    return errors


def response_errors(model: dict[str, Any], expected_count: int, *, expected_version: str = RESPONSE_VERSION, experiment_indices=()) -> list[str]:
    errors = list(model.get("contract_errors") or [])
    if model.get("response_version") != expected_version:
        errors.append("RESPONSE_VERSION_UNSUPPORTED")
    if not isinstance(model.get("overall"), str) or model["overall"] not in OVERALL_LEVELS:
        errors.append("OVERALL_INVALID")
    rows = model.get("segments")
    if not isinstance(rows, list):
        return errors + ["SEGMENTS_NOT_LIST"]
    indices = []
    for row in rows:
        errors.extend(f"S{row.get('index', '?')}:{error}" if isinstance(row, dict) else error
                      for error in row_errors(row))
        if (expected_version != RESPONSE_VERSION and isinstance(row, dict)
                and normalize_match_level(row) in {"DIRECT", "ACCEPTABLE_DEGRADED"}
                and type(row.get("confidence")) in (int, float) and row["confidence"] < .6):
            errors.append("AI_EDITOR_POSITIVE_CONFIDENCE_LOW")
        if isinstance(row, dict) and type(row.get("index")) is int:
            indices.append(row["index"])
            if row["index"] in experiment_indices:
                events = row.get("visible_experiment_events")
                if not valid_experiment_observations(events):
                    errors.append(f"S{row['index']}:EXPERIMENT_EVENT_OBSERVATIONS_REQUIRED")
    if sorted(indices) != list(range(1, expected_count + 1)):
        errors.append("BATCH_INDEX_COVERAGE_INVALID")
    return sorted(set(errors))


def report_status(report: Any, *, report_dir=None, manifest=None) -> str:
    """Check report consistency; consumers must also call report_matches_draft.

    A self-contained report cannot authenticate its own selection digest. Only
    the draft identity in report_matches_draft anchors that digest to the build.
    """
    try:
        status = _report_status(report)
        if status not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}:
            return status
        ai_mode = report.get("version") == AI_REPORT_VERSION
        if ai_mode and not (report_dir or manifest):
            return status
        # Historical rule-only reports did not bind category or experiment cuts.
        # Recover those facts from the retained selection and task before accepting one.
        match = {}
        path = Path(report_dir) / "shot_match_report.json" if report_dir else None
        if path and path.is_file():
            match = json.loads(path.read_text(encoding="utf-8-sig"))
        current = manifest or {}
        scope = {**(match.get("manifest") or {}), **(current.get("manifest") or {}), **current}
        if manifest and (current.get("script") or current.get("full_script")):
            scoped = current.get("manifest") or {}
            scope["sentence_visual_approvals"] = current.get("sentence_visual_approvals", scoped.get("sentence_visual_approvals", []))
        if manifest and (manifest.get("script") or manifest.get("full_script")):
            scope["full_script"] = manifest.get("script") or manifest["full_script"]
        segments = bound_selection(report) if ai_mode else match.get("segments") or []
        contract = _ai_contract()
        if any(s.get("approved_visual") for s in segments) or scope.get("sentence_visual_approvals"):
            for index, seg in enumerate(segments, 1):
                text = seg.get("claim_text") or seg.get("text") or ""
                if contract.sentence_visuals.approved(scope, index, text) != seg.get("approved_visual"):
                    return "UNCERTIFIED"
        if ai_mode:
            return status
        rows = segments or scope.get("segments") or report.get("model_review") or []
        declared = [{"text": r.get("claim") or r.get("text") or ""} for r in rows if isinstance(r, dict)]
        scope.setdefault("category", report.get("category"))
        scope.setdefault("segments", declared)
        if contract.rules_identity(scope)["category"] == "zhipin":
            mapping = {m["claim_index"]: m["draft_segments"] for m in report.get("segment_map") or []}
            for row in report.get("model_review") or []:
                index = row["index"]
                segment = segments[index - 1] if type(index) is int and 1 <= index <= len(segments) else {}
                spoken = segment.get("claim_text") or segment.get("text") or row.get("claim") or ""
                if contract.paper_claims.requirements(spoken):
                    cuts = contract.paper_claims.selected_cut_bounds(segment, mapping.get(index, []))
                    if not contract.paper_claims.output_coverage(spoken, row.get("paper_output_evidence"), cuts)[0]:
                        return "UNCERTIFIED"
        if scope.get("full_script"):
            rows = [*rows, {"text": scope["full_script"]}]
        if _ai_contract().unverified_absorption_indices(scope, rows, allow_reviewed=False):
            return "UNCERTIFIED"
        return status
    except (OSError, RuntimeError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "FAIL" if isinstance(report, dict) and report.get("status") == "FAIL" else "UNCERTIFIED"


def report_matches_draft(report: Any, draft: Path) -> bool:
    """Bind a persisted QC report to the exact draft directory it inspected."""
    if not isinstance(report, dict) or not isinstance(report.get("draft"), str):
        return False
    try:
        expected = Path(draft).resolve(strict=True)
        recorded = Path(report["draft"]).resolve(strict=True)
        ai_identity = expected / "ai_selection_identity.json"
        mode = report.get("selection_mode")
        if ai_identity.exists() or (mode in {"ai_editor", "rule_then_ai"}
                                    and report.get("version") == AI_REPORT_VERSION):
            if not ai_identity.is_file() or mode not in {"ai_editor", "rule_then_ai"}:
                return False
            identity = json.loads(ai_identity.read_text(encoding="utf-8"))
            if (identity.get("selection_mode") != mode
                    or identity.get("selection_sha256") != report.get("selection_sha256")
                    or report.get("response_contract", {}).get("version") != AI_RESPONSE_VERSION):
                return False
            bound_selection(report)
            if (not _current_editorial_rules(report) or identity.get("editorial_rules") != report.get("editorial_rules")
                    or identity.get("policy_version") != report.get("policy_version")):
                return False
        if not _source_bindings_valid(report, expected):
            return False
        return (expected.is_dir() and recorded.is_dir()
                and os.path.normcase(str(expected)) == os.path.normcase(str(recorded))
                and report.get("draft_metadata_sha256") == draft_metadata_fingerprint(expected)
                and report.get("draft_metadata_sha256_final") == report.get("draft_metadata_sha256"))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _report_status(report: Any) -> str:
    """Recheck the same evidence contract before trusting a persisted PASS."""
    if not isinstance(report, dict):
        return "UNCERTIFIED"
    rows = report.get("model_review") or []
    if report.get("status") == "FAIL" or any(
            isinstance(row, dict) and normalize_match_level(row) == "MISMATCH" for row in rows):
        return "FAIL"
    status = report.get("status")
    if (str(report.get("delivery_mode") or "").strip().lower() != "formal"
            or report.get("release_eligibility") == "PREVIEW_ONLY" and status == "PASS"
            or status not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}):
        return "UNCERTIFIED"
    if not _source_bindings_valid(report):
        return "UNCERTIFIED"
    contract = report.get("response_contract") or {}
    mode = report.get("selection_mode")
    ai_mode = (mode == "ai_editor" or (mode == "rule_then_ai" and report.get("version") == AI_REPORT_VERSION))
    expected_report = AI_REPORT_VERSION if ai_mode else REPORT_VERSION
    expected_response = AI_RESPONSE_VERSION if ai_mode else RESPONSE_VERSION
    if (report.get("version") != expected_report or not isinstance(contract, dict)
            or contract.get("version") != expected_response
            or contract.get("status") != "VALIDATED" or contract.get("errors") != []):
        return "UNCERTIFIED"
    metadata_hash = report.get("draft_metadata_sha256")
    if (not isinstance(metadata_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", metadata_hash)
            or report.get("draft_metadata_sha256_final") != metadata_hash):
        return "UNCERTIFIED"
    if not isinstance(rows, list) or not rows or any(row_errors(row) for row in rows):
        return "UNCERTIFIED"
    if ai_mode:
        if (not _current_editorial_rules(report)
                or not re.fullmatch(r"[0-9a-f]{64}", str(report.get("selection_sha256") or ""))
                or report.get("ai_final_windows_verified" if mode == "rule_then_ai" else "final_windows_verified") is not True
                or report.get("issues")
                or any(row.get("confidence", 0) < .6 for row in rows)):
            return "UNCERTIFIED"
        segments = bound_selection(report)
        mapping = {m["claim_index"]: m["draft_segments"] for m in report.get("segment_map") or []}
        paper = _ai_contract().paper_claims
        for row in rows:
            index = row["index"]
            if type(index) is not int or not 1 <= index <= len(segments):
                return "UNCERTIFIED"
            segment = segments[index - 1]
            spoken = segment.get("claim_text") or segment.get("text") or ""
            if segment.get("approved_visual"):
                cuts = paper.selected_cut_bounds(segment, mapping.get(index, []))
                if (row.get("evidence_basis") != "approved_presentation" or row.get("approved_show_seen") is not True
                        or not _ai_contract().sentence_visuals.presentation_visible(segment["approved_visual"],
                              row.get("approved_show_evidence"), cuts, row.get("observed"))):
                    return "UNCERTIFIED"
            elif report["editorial_rules"].get("category") == "zhipin" and paper.requirements(spoken):
                cuts = paper.selected_cut_bounds(segment, mapping.get(index, []))
                covered, _ = paper.output_coverage(spoken, row.get("paper_output_evidence"), cuts)
                if not covered or paper.count_disputed(covered, row.get("observed")):
                    return "UNCERTIFIED"
        if (_ai_contract().unverified_absorption_indices(report["editorial_rules"], segments)
                or experiment_selection_errors(segments, rows) or presentation_selection_errors(segments, rows)):
            return 'UNCERTIFIED'
    indices = [row["index"] for row in rows]
    mapping = report.get("segment_map") or []
    batches = report.get("batch_results") or []
    if (not isinstance(mapping, list) or not isinstance(batches, list) or not batches
            or any(not isinstance(row, dict) for row in mapping + batches)):
        return "UNCERTIFIED"
    expected = [row.get("claim_index") for row in mapping]
    batch_indices = [index for batch in batches for index in (batch.get("claim_indices") or [])]
    if (len(set(indices)) != len(indices) or sorted(indices) != sorted(expected)
            or sorted(indices) != sorted(batch_indices)
            or any(batch.get("status") != "PARSED" or batch.get("contract_status") != "VALIDATED"
                   for batch in batches)
            or contract.get("validated_batches") != list(range(1, len(batches) + 1))):
        return "UNCERTIFIED"
    levels = [normalize_match_level(row) for row in rows]
    if "INSUFFICIENT_EVIDENCE" in levels or (status == "PASS" and any(level != "DIRECT" for level in levels)):
        return "UNCERTIFIED"
    return status
