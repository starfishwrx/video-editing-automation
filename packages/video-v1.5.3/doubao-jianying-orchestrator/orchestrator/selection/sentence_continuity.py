"""Bounded joint review after short complete single-sentence alternatives fail."""
from __future__ import annotations

import copy
import json
from .. import ai_editor_contract as contract
from ..policy import continuity
from . import timing_contract as timing


def _anchors(plan, members, index, shots, rejected):
    """Retrieve joint-review windows; these anchors never certify a member."""
    histories = {s["index"]: list(plan["reviews"].get(str(s["index"]), [])) + [
        r for r in plan.get("window_reviews", {}).values()
        if r.get("sentence_index") == s["index"] and not r.get("continuity_key")] for s in members}
    reviewed = [{r["shot_id"]: r for r in plan["reviews"].get(str(s["index"]), [])
        if r.get("verdict") == "matched" and r.get("same_product") is True
        and r["shot_id"] not in rejected.get(s["index"], set())} for s in members]
    shared = sorted(set.intersection(*(set(r) for r in reviewed)) & shots.keys())
    seen = set()
    def novel(sid, windows):
        key = (sid, min(e["start"] for e in windows), max(e["end"] for e in windows))
        if key in seen:
            return False
        seen.add(key)
        return True
    for sid in shared:
        source = contract.source_identity(shots[sid]["video"])
        if all(r[sid].get("source_identity") == source for r in reviewed):
            windows = [ev for r in reviewed for ev in r[sid]["evidence_intervals_us"]]
            if novel(sid, windows):
                yield sid, windows
    # A failed sentence can supply an already observed compact anchor while a
    # neighbour has never seen that source. Only a NEW full joint pixel review
    # may establish the neighbour's phase; known negative observations still bar it.
    anchors = [r for r in plan.get("window_reviews", {}).values()
        if r.get("sentence_index") == index and not r.get("continuity_key")] + list(
            plan["reviews"].get(str(index), []))
    for anchor in anchors:
        sid = anchor.get("shot_id")
        if (sid not in shots or anchor.get("verdict") != "matched" or anchor.get("same_product") is not True
                or any(sid in rejected.get(s["index"], set()) for s in members)
                or any(r.get("shot_id") == sid and r.get("verdict") in {"mismatch", "uncertain"}
                       for rows in histories.values() for r in rows)):
            continue
        source = contract.source_identity(shots[sid]["video"])
        if anchor.get("source_identity") == source and novel(sid, anchor["evidence_intervals_us"]):
            yield sid, anchor["evidence_intervals_us"]


def proposals(manifest, analysis, plan, index, narration_timing):
    """Shared candidate IDs retrieve possibilities; only joint pixels certify them."""
    from .ai_editor import qc_rejected_shots
    rejected = qc_rejected_shots(manifest)
    shots = {s["shot_id"]: s for s in analysis["shots"]}
    sentences = {s["index"]: s for s in plan["sentences"]}
    originals = timing.split_sentences(manifest)
    pad = timing.us_of(manifest.get("tail_pad_s"), 0)
    for length in range(2, continuity.MAX_MEMBERS + 1):
        for first in range(max(1, index - length + 1), min(index, len(sentences) - length + 1) + 1):
            members = [sentences[i] for i in range(first, first + length)]
            if any(s["kind"] != "selling_point" for s in members):
                continue
            if any(r.get("continuity_key") for r in plan["rows"] if first <= r["index"] < first + length):
                continue
            for sid, windows in _anchors(plan, members, index, shots, rejected):
                shot = shots[sid]
                source = contract.source_identity(shot["video"])
                durations = [timing.duration_for(narration_timing, s["index"], segment=originals[s["index"] - 1]) for s in members]
                if any(certainty == "estimated" or duration <= 0 for duration, certainty in durations):
                    continue
                total = sum(d + pad for d, _ in durations)
                material_us = shot.get("material_duration_us")
                solution = timing.solve_window(timing.WindowRequest(
                    source_in_us=round(shot["source_start"] * 1e6), source_out_us=round(shot["source_end"] * 1e6),
                    video_duration_us=material_us if type(material_us) is int and material_us > 0 else None,
                    audio_duration_us=total, tail_pad_us=0,
                    head_guard_us=timing.us_of(manifest.get("head_trim_s"), 300_000),
                    tail_guard_us=timing.us_of(manifest.get("tail_trim_s"), 200_000),
                    evidence_start_us=min(ev["start"] for ev in windows), evidence_end_us=max(ev["end"] for ev in windows),
                    speed_min=1, speed_max=1))
                if not solution.ok:
                    continue
                cursor, allocated = solution.source_start_us, []
                for sentence, (duration, _) in zip(members, durations):
                    target = duration + pad
                    allocated.append({k: copy.deepcopy(sentence[k]) for k in ("index", "text", "kind", "selling_points", "scene_ids") if k in sentence})
                    allocated[-1].update(audio_duration_us=duration, target_us=target, start_us=cursor, end_us=cursor + target)
                    cursor += target
                yield {"product": manifest.get("product"), "category": manifest.get("category"),
                       "rules": contract.rules_identity(manifest), "shot_id": sid, "source_identity": source,
                       "scene_us": {"start": round(shot["source_start"] * 1e6), "end": round(shot["source_end"] * 1e6)},
                       "window_us": {"start": solution.source_start_us, "end": solution.source_end_us}, "members": allocated}


def review_group(manifest, context, shot, directory):
    from . import ai_editor as editor
    media = editor._review_media({**shot, "source_start": context["window_us"]["start"] / 1e6,
                                 "source_end": context["window_us"]["end"] / 1e6}, directory, dense=True)
    if media["before"] != context["source_identity"]:
        raise ValueError("CONTINUITY_SOURCE_CHANGED")
    prompt = contract.policy_prompt(manifest) + "\n联合复核连续演示；原素材时间戳，画面原速，不允许改变以下逐句区间。\n"
    prompt += json.dumps(context, ensure_ascii=False) + """
只有相邻句确实延续同一具体卖点、同商品同一次演示且语义顺畅时才能matched。
相同文件/候选ID不证明同一次实验；不能把不同卖点、不同商品、两次重复实验、跳切或无关促销并组。
检查组合窗口完整保留必要动作及可读结果，每句对应的源区间实际展示该句承担的阶段；
禁止结果口播时还在无关准备，禁止把只能组末看到的结果宣称已在前句出现。跨句处必须画面连续。
不能仅因总长度放得下就通过。看不清阶段、商品/实验对应或结果可读性时uncertain。
反渗、回流、干爽、不黏或黏腻的按压验证只认纸巾接触巾体并提起。手掌、手指、手离开、手掌干净都不是按压验证。组内有这类句子却看不见纸巾时，complete_action 必须为 false，verdict 不得 matched。
输出JSON：{"verdict":"matched|mismatch|uncertain","same_product":true,"same_demonstration":true,
"same_selling_point":true,"semantic_continuity":true,"complete_action":true,"result_preserved":true,
"shared_point":"具体共同卖点","product_observation":"同商品可见依据","demonstration_observation":"同次连续演示可见依据",
"observation":"完整动作结果事实","reason":"组合和节奏依据","confidence":0.9,
"complete_intervals_us":[{"start":0,"end":4000000}],
"members":[{"index":1,"verdict":"matched","same_product":true,"basis":"editorial_fit",
"phase":"本句画面阶段","observation":"该切片实际事实","reason":"与本句对应的依据",
"supported_point_ids":["p1"],"time_sensitive":false,"scene_ids":[],
"evidence_intervals_us":[{"start":0,"end":2000000}]}]}。
members每句恰好一行并按原序，证据区间只填本句切片内实际观察到的阶段，不复制超出切片的旧证据。
完整动作由整组连续窗口承载，不冒称每片都独立完整。结果必须完整原速保留；无需重做素材识别。
"""
    result = editor._call(prompt, directory / "continuity" / (contract.digest(context)[:24] + ".json"), media["sheet"])
    if contract.source_identity(shot["video"]) != context["source_identity"]:
        raise ValueError("CONTINUITY_SOURCE_CHANGED")
    group = {"schema": continuity.SCHEMA, "input": context, "review": result,
             "frame_times_us": [round(t * 1e6) for t in media["times"]]}
    return continuity.validate(group)


def install(plan, key, group):
    """Store new observations separately; original wide-window facts stay untouched."""
    if continuity.is_experiment(group):
        from .experiment_groups import install as install_experiment
        return install_experiment(plan, key, group)
    continuity.validate(group)
    plan.setdefault("continuity_reviews", {})[key] = copy.deepcopy(group)
    context = group["input"]
    for member, observed in zip(context["members"], group["review"]["members"]):
        index = member["index"]
        window_key = key + ":" + str(index)
        plan.setdefault("window_reviews", {})[window_key] = {
            **copy.deepcopy(observed), "sentence_index": index, "shot_id": context["shot_id"],
            "confidence": group["review"]["confidence"], "source_identity": context["source_identity"],
            "review_window_us": {"start": member["start_us"], "end": member["end_us"]},
            "continuity_key": key}
        row = {"index": index, "continuity_key": key, "reason": observed["reason"], "parts": [{
            "shot_id": context["shot_id"], "audio_duration_us": member["audio_duration_us"],
            "source_start_us": member["start_us"], "window_review_key": window_key}]}
        plan["rows"] = [row if r["index"] == index else r for r in plan["rows"]]


def validate_plan(manifest, analysis, plan, narration_timing):
    shots = {s["shot_id"]: s for s in analysis["shots"]}
    originals = timing.split_sentences(manifest)
    groups = {}
    for row in plan["rows"]:
        key = row.get("continuity_key")
        if not key:
            continue
        group = continuity.validate(plan.get("continuity_reviews", {})[key])
        context = group["input"]
        if continuity.is_experiment(group):
            from . import experiment_groups
            experiment_groups.validate_plan(manifest, analysis, plan, group, narration_timing)
            # Rebuild the selected rows from the immutable allocation and compare.
            expected = copy.deepcopy(plan)
            experiment_groups.install(expected, key, group)
            for member in context["members"]:
                current = next(r for r in plan["rows"] if r["index"] == member["index"])
                target = next(r for r in expected["rows"] if r["index"] == member["index"])
                if current != target:
                    raise ValueError("CONTINUITY_INPUT_CHANGED")
            groups[key] = group
            continue
        if (context["product"] != manifest.get("product") or context["category"] != manifest.get("category")
                or context["rules"] != contract.rules_identity(manifest)
                or context["source_identity"] != contract.source_identity(shots[context["shot_id"]]["video"])):
            raise ValueError("CONTINUITY_INPUT_CHANGED")
        for member in context["members"]:
            index = member["index"]
            sentence = next(s for s in plan["sentences"] if s["index"] == index)
            duration, _ = timing.duration_for(narration_timing, index, segment=originals[index - 1])
            selected = next(r for r in plan["rows"] if r["index"] == index)
            if (any(member.get(k) != sentence.get(k) for k in ("text", "kind", "selling_points", "scene_ids"))
                    or duration != member["audio_duration_us"]
                    or duration + timing.us_of(manifest.get("tail_pad_s"), 0) != member["target_us"]
                    or selected.get("continuity_key") != key):
                raise ValueError("CONTINUITY_INPUT_CHANGED")
        groups[key] = group
    return groups


def repair(manifest, analysis, plan, directory, failure, narration_timing, *, member_indices=None):
    from . import ai_editor as editor
    from .ai_editor_recovery import failure_fields, repairable_index
    attempts = plan.setdefault("continuity_attempts", {})
    for context in proposals(manifest, analysis, plan, failure.index, narration_timing):
        if member_indices is not None and {m["index"] for m in context["members"]} != set(member_indices):
            continue
        key = contract.digest(context)[:24]
        group = plan.get("continuity_reviews", {}).get(key)
        if not group:
            if key in attempts or sum(a["index"] == failure.index for a in attempts.values()) >= 3:
                continue
            attempts[key] = {"index": failure.index, "status": "reviewing"}
            editor._save(directory / "plan.json", plan)
            try:
                shot = next(s for s in analysis["shots"] if s["shot_id"] == context["shot_id"])
                group = review_group(manifest, context, shot, directory)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                attempts[key].update(status="rejected", reason=str(exc))
                editor._save(directory / "plan.json", plan)
                if failure_fields(str(exc)).get("failure_class") == "vision":
                    raise
                continue
            plan.setdefault("continuity_reviews", {})[key] = group
            attempts[key]["status"] = "reviewed"
        trial = copy.deepcopy(plan)
        install(trial, key, group)
        try:
            editor._assemble(manifest, analysis, trial, narration_timing=narration_timing, verify_sources=True)
        except (ValueError, KeyError, TypeError, OSError) as other:
            other_index = repairable_index(other)
            members = {m["index"] for m in context["members"]}
            if other_index is None or other_index in members:
                if not str(other).startswith(("AI_EDITOR_REPEATED_SOURCE_WINDOW:", "AI_EDITOR_SOURCE_REUSE_LIMIT:")):
                    continue
                # The public error shows only the first conflict. A hidden
                # group/structure conflict must not ride along with this exception.
                try:
                    checked = editor._assemble(manifest, analysis, trial, narration_timing=narration_timing,
                                               verify_sources=True, validate_sequence=False)
                except (ValueError, KeyError, TypeError, OSError):
                    continue
                from .ai_editor_recovery import only_outside_sequence_conflicts
                if not only_outside_sequence_conflicts(checked["segments"], manifest, members):
                    continue
        plan.update(trial)
        plan.setdefault("continuity_repairs", []).append({"indices": [m["index"] for m in context["members"]],
                                                          "reason": str(failure), "review_key": key})
        editor._save(directory / "plan.json", plan)
        return True
    return False
