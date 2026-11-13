"""Review camera returns and repeated ordinary displays on actual placed windows."""

from __future__ import annotations

import json

from .. import ai_editor_contract as contract
from ..runtime import platform_env
from ..vision import material_cache, vision_analyzer
from . import experiment_groups, shot_notes


def groups(segments, plan, manifest):
    rows = {r["index"]: r for r in plan["rows"]}
    sentences = {s["index"]: s for s in plan["sentences"]}
    experiment_members = {
        m["index"]
        for group in experiment_groups.groups(plan["sentences"], require_rewet=False)
        for m in group
    }
    result = []
    for index, segment in enumerate(segments, 1):
        parts = segment.get("span_parts") or [segment]
        sentence, row = sentences[index], rows[index]
        for cut in parts:
            window = (round(cut["source_start"] * 1e6), round(cut["source_end"] * 1e6))
            if result and result[-1]["video"] == cut["video"] and result[-1]["end"] == window[0]:
                group = result[-1]
            else:
                group = {"indices": [], "video": cut["video"], "start": window[0], "editable": True}
                result.append(group)
            group["end"] = window[1]
            group["indices"].append(index)
            group["editable"] &= (
                len(parts) == 1
                and sentence["kind"] in {"hook", "product_intro"}
                and not sentence.get("selling_points")
                and not sentence.get("scene_ids")
                and not cut.get("time_sensitive")
                and index not in plan.get("_frozen_segments", {})
                and index not in experiment_members
                and not (
                    sentence.get("experiment_role") == "bridge"
                    and contract.rules_identity(manifest)["category"] == "weipin"
                )
                and not row.get("continuity_key")
                and not segment.get("visual_requirements")
                and not shot_notes.note_for(manifest, index, segment)
                and not sentence.get("action_continuation")
                and sentence.get("experiment_role") not in {"absorption", "rewet", "followup"}
            )
    return result


def sheet_for(cuts, directory, media, context=()):
    tiles = []
    for label, cut in cuts:
        frames = media(cut, directory)["frames"]
        for j in range(4):
            f = frames[round(j * (len(frames) - 1) / 3)]
            tiles.append({**f, "label": f"{label} {f['timestamp']:.2f}s"})
    for label, cut in context:
        frames = media(cut, directory)["frames"]
        f = frames[len(frames) // 2]
        tiles.append({**f, "label": label})
    params = {"columns": 4, "tile_width": 180, "tile_height": 248, "fit": True}
    path = material_cache.cached_sheet(
        tiles,
        directory,
        prefix="camera",
        parameters=params,
        prepare=lambda target: vision_analyzer._write_sheet(
            tiles, target, **params, ffmpeg=platform_env.find_ffmpeg()[0]
        ),
    )
    if path is None:
        raise ValueError("CAMERA_REVIEW_FRAMES_UNAVAILABLE")
    return path


def group_cut(group):
    return {"video": group["video"], "source_start": group["start"] / 1e6, "source_end": group["end"] / 1e6}


def find_repeats(groups, segments, directory, media, call, *, audit=None):
    if len(groups) < 2 or not any(g["editable"] for g in groups):
        return []
    found = set()
    batches = []
    for anchor, group in enumerate(groups):
        others = [b for b in range(anchor + 1, len(groups)) if group["editable"] or groups[b]["editable"]]
        for offset in range(0, len(others), 4):
            batches.append((offset, anchor, others[offset : offset + 4]))
    # First cover nearby cuts across the script, then longer returns.
    batches.sort(key=lambda item: (item[0], item[1]))
    total_pairs = sum(len(batch) for _, _, batch in batches)
    if len(batches) > 12:
        nearby = [item for item in batches if item[0] == 0]
        far = [item for item in batches if item[0] > 0]

        def sample(items, limit):
            if len(items) <= limit:
                return items
            return [items[round(n * (len(items) - 1) / (limit - 1))] for n in range(limit)]

        first = sample(nearby, 8 if len(far) >= 4 else 12 - len(far))
        batches = first + sample(far, 12 - len(first))
    if audit is not None:
        audit.update(
            eligible_pairs=total_pairs,
            screened_pairs=sum(len(batch) for _, _, batch in batches),
            detection_calls=len(batches),
        )
    for offset, anchor, batch in batches:
        group = groups[anchor]
        cuts = [("REF", group_cut(group))] + [(f"B{n}", group_cut(groups[b])) for n, b in enumerate(batch)]
        sheet = sheet_for(cuts, directory, media)
        prompt = (
            "首行REF是参考连续镜头。以下每行（B0起）独立与首行比，四列是时间顺序。"
            "逐行给出两个布尔值，可同时成立。camera：相同固定背景参照物、视角、景别和产品布局近似；"
            "仅同产品或颜色不算。action：两行各自重新做了相似普通展示，如双手正面托举一排产品、"
            "反复抚摸包装。即使室内室外不同、持有数量不同或文案不同，也须记录这种展示重复。"
            "同一动作连续推进则填false。不要用卖点文案替代画面判断。输出JSON "
            '{"rows":[{"b":0,"camera":true,"action":false}]}，每行都要回答。'
        )
        result = call(prompt, directory / "camera_variety" / f"anchor_{anchor}_{offset}.json", sheet)
        rows = result.get("rows")
        if (
            not isinstance(rows, list)
            or len(rows) != len(batch)
            or any(
                not isinstance(row, dict)
                or type(row.get("b")) is not int
                or any(type(row.get(kind)) is not bool for kind in ("camera", "action"))
                for row in rows
            )
            or {row["b"] for row in rows} != set(range(len(batch)))
        ):
            if audit is not None:
                audit["incomplete_batches"] = audit.get("incomplete_batches", 0) + 1
            continue
        for row in rows:
            for kind in ("camera", "action"):
                if row[kind]:
                    found.add((anchor, batch[row["b"]], kind))
    return sorted(
        found,
        key=lambda pair: (
            not (pair[2] == "camera" and pair[1] > pair[0] + 1),
            pair[2] != "camera",
            pair[1],
            pair[0],
        ),
    )


def options(group, menu, used, reviews, manifest):
    from . import shot_analyzer

    """A reviewed parent can propose consecutive real-time cuts; each earns its own receipt."""
    for c in menu:
        sid = c["shot_id"]
        if sid in used or c.get("time_sensitive") or (c.get("usability") or {}).get("status") == "REJECTED":
            continue
        bounds = reviews[sid].get("review_window_us") or {}
        if not bounds or not c.get("evidence_intervals_us"):
            continue
        technical = c["technical_option"]
        head = shot_analyzer._number(manifest.get("head_trim_s"), shot_analyzer.DEFAULT_HEAD_TRIM_S)
        tail = shot_analyzer._number(manifest.get("tail_trim_s"), shot_analyzer.DEFAULT_TAIL_TRIM_S)
        phase = contract.presentation.get(reviews[sid].get("presentation_observation"))
        if len(group["indices"]) > 1 and phase:
            # Individually certified cuts cannot each carry the same full motion phase.
            continue
        first = max(bounds["start"], technical["original_scene_start_us"] + round(head * 1e6))
        last = min(bounds["end"], technical["original_scene_end_us"] - round(tail * 1e6))
        duration = group["duration"]
        if last - first < duration:
            continue
        start = (
            round(c["technical_option"]["source_start"] * 1e6)
            if c["technical_option"].get("ok")
            else c["evidence_intervals_us"][0]["start"]
        )
        start = max(first, min(start, last - duration))
        if phase:
            start = phase["start_us"]
            if (
                start < first
                or start + duration > last
                or contract.presentation.cut_errors(phase, start, start + duration)
            ):
                continue
        yield {"shot_id": sid, "start": start, "end": start + duration, "observation": c["observation"]}


def compare(groups, pair, target, choices, shots, directory, media, call):
    a, b, kind = pair
    # Include immediate neighbours of the displaced shot to protect unlabelled cross-file continuity.
    ids = sorted({a, b, *[n for n in (target - 1, target + 1) if 0 <= n < len(groups)]})
    inputs = [(f"G{k}", group_cut(groups[k])) for k in ids]
    for j, c in enumerate(choices):
        inputs.append(
            (
                f"ALT{j}",
                {**shots[c["shot_id"]], "source_start": c["start"] / 1e6, "source_end": c["end"] / 1e6},
            )
        )
    other_groups = [n for n in range(len(groups)) if n not in ids]
    if len(other_groups) > 12:
        other_groups = [other_groups[round(i * (len(other_groups) - 1) / 11)] for i in range(12)]
    context = [(f"OTHER G{n}", group_cut(groups[n])) for n in other_groups]
    sheet = sheet_for(inputs, directory, media, context)
    prompt = (
        f"图中每行从左到右是一个实际镜头，原片顺序编号G。检查G{a}和G{b}是否"
        f"{'切回相似机位和构图' if kind == 'camera' else '重复同样的托举、抚摸或陈列展示动作'}。"
        f"只判断指定的G{a}与G{b}这一对；邻镜只用于排除动作承接，不得用邻镜替代这对。拟换G{target}。原镜承接邻镜继续动作、提供独有新卖点或没有重复则保留。"
        "如果重复，从ALT中选一条，既改变重复的角度/距离/构图或动作，又有清楚的同款商品、"
        "完整动作或有效展示。不接受仅换包装颜色，也不要在邻镜之间制造相同展示。"
        "换镜必须让主要展示动作更有变化，优先平铺扫拍、拆开或展开等与反复托举不同的真实表现。返回JSON，reason说明可见背景、取景和动作，relation为repeat|progression|new_fact|different|unknown，"
        f"若relation是repeat，还需返回first={a}、second={b}、两行各自的画面观察first_observation和second_observation。"
        "choice为所选ALT的整数编号，没有合适的填null。\n"
        + json.dumps(
            {"target_lines": groups[target]["texts"], "rows": [n for n, c in inputs]}, ensure_ascii=False
        )
    )
    prompt += (
        "\n最后标OTHER的每个小格是本稿其余镜头的实际中帧。逐一对照所有参照，包括邻镜。"
        "给出novel_setup或novel_action，以及preserves_information和avoids_new_repeat四个JSON布尔值。"
        "候选不能新增相邻或隔镜回切的相似背景、布局和展示动作；原画面独有的信息，如同框展示不同款式，"
        "若原句需要或有独立展示价值，替换也必须保留。比较不确定时不换。"
    )
    key = contract.digest([prompt, contract.file_sha256(sheet)])[:24]
    raw = call(prompt, directory / "camera_variety" / f"compare_{key}.json", sheet)
    choice = raw.get("choice")
    if (
        raw.get("relation") == "repeat"
        and raw.get("first") == a
        and raw.get("second") == b
        and all(
            isinstance(raw.get(k), str) and raw[k].strip()
            for k in ("first_observation", "second_observation")
        )
        and raw.get("preserves_information") is True
        and raw.get("avoids_new_repeat") is True
        and raw.get("novel_setup" if kind == "camera" else "novel_action") is True
        and isinstance(raw.get("reason"), str)
        and raw["reason"].strip()
        and type(choice) is int
        and 0 <= choice < len(choices)
    ):
        return choices[choice]
    return None


def shortlist(group, choices, anchors, directory, call):
    if len(choices) <= 3:
        return choices
    prompt = (
        "为普通产品展示重选连续镜头。根据候选观察，推荐与原片机位、构图和展示动作差异最大的三条，"
        "按与句义吻合程度排序。优先不同的实际展示方式，避免仅换场景继续双手托举。返回JSON "
        '{"ids":["shot_id"]}。\n'
        + json.dumps(
            {
                "lines": group["texts"],
                "original": anchors,
                "options": [(c["shot_id"], c["observation"]) for c in choices],
            },
            ensure_ascii=False,
        )
    )
    raw = call(prompt, directory / "camera_variety" / f"rank_{contract.digest(prompt)[:20]}.json")
    by_id = {c["shot_id"]: c for c in choices}
    return [
        by_id[k] for k in dict.fromkeys(k for k in raw.get("ids", []) if isinstance(k, str) and k in by_id)
    ][:3]


def fits_revised_layout(layout, changed, target, candidate, shot, directory, media, call):
    others = sorted(changed | {k for k in (target - 1, target + 1) if 0 <= k < len(layout)})
    if not others:
        return True
    cuts = [
        ("NEW", {**shot, "source_start": candidate["start"] / 1e6, "source_end": candidate["end"] / 1e6})
    ]
    cuts += [(f"N{n}", group_cut(layout[k])) for n, k in enumerate(others)]
    sheet = sheet_for(cuts, directory, media)
    prompt = (
        "首行NEW是拟换入的镜头。其余各行是同稿已改过的镜头及拟换位置邻镜，四帧从左向右。逐行对照。"
        "即使隔了几镜，只要NEW与任一参照的景别、角度、主体摆放相似（特别是俯拍同桌平铺多包）或重新做类似托举、"
        "触摸，且未推进同一次动作，则拒绝。只返回JSON，"
        '{"distinct_from_every_neighbor":true,"observation":"逐个指出景别、布局、动作上可见的区别"}。'
    )
    key = contract.digest([prompt, contract.file_sha256(sheet)])[:24]
    raw = call(prompt, directory / "camera_variety" / f"neighbors_{key}.json", sheet)
    return raw.get("distinct_from_every_neighbor") is True and bool(
        str(raw.get("observation") or "").strip()
    )


def recall_displays(manifest, plan, group, shots, directory, editor, used, anchors):
    """A bounded look outside the display pool when its remaining scenes all repeat the same setup."""
    indices = group["indices"]
    reviewed = {r["shot_id"] for r in plan["display_reviews"]}
    rejected = editor.qc_rejected_shots(manifest)
    choices = [
        s
        for s in shots.values()
        if s["shot_id"] not in reviewed
        and s["video"] not in used
        and s.get("role") == "product_display"
        and s.get("duration", 0) >= group["duration"] / 1e6
        and all(s["shot_id"] not in rejected.get(n, set()) for n in indices)
    ]
    if not choices:
        return []
    prompt = (
        "为这段连续的普通商品展示找最多两条构图和动作不同的素材。优先俯拍平铺、扫拍完整包装、"
        "已经散开或陈列的整套商品。下列original是当前参照，请避开它的机位和动作。"
        "优先镜头内容稳定且覆盖口播、无遮挡。只返回JSON "
        '{"ids":["shot_id"]}。'
        + json.dumps(
            {
                "lines": group["texts"],
                "original": anchors,
                "candidates": [
                    (s["shot_id"], s.get("setup_id"), str(s.get("description", ""))[:90])
                    for s in choices[:80]
                ],
            },
            ensure_ascii=False,
        )
    )
    raw = editor._call(prompt, directory / "camera_variety" / f"recall_{contract.digest(prompt)[:20]}.json")
    ids = {s["shot_id"] for s in choices[:80]}
    result = []
    for sid in dict.fromkeys(i for i in raw.get("ids", []) if isinstance(i, str) and i in ids):
        if len(result) >= 2:
            break
        result.append(sid)
    for sid in result:
        shared = {
            "index": 0,
            "kind": "product_intro",
            "text": "同商品通用产品展示池",
            "selling_points": [],
            "audio_durations_us": [group["duration"]],
        }
        review = editor._review(manifest, shared, shots[sid], directory, dense=True)
        plan["display_reviews"].append(review)
    return result


def verify_join(group, candidate, shot, directory, editor):
    members = group["indices"]
    cursor = candidate["start"]
    windows = []
    for n, line in zip(members, group["texts"]):
        end = cursor + group["durations"][n] + group["pad"]
        windows.append((n, line, cursor, end))
        cursor = end
    media = editor._review_media(
        {**shot, "source_start": candidate["start"] / 1e6, "source_end": cursor / 1e6},
        directory,
        dense=True,
    )
    prompt = (
        "按实际帧和原素材微秒时间核查每句切点，确认整组是同款商品同一段自然连续的展示，跨句无跳切、"
        "动作重置或重复拿起，动作结果仍然完整可读，且画面适合各句。稳定陈列的连续移动取景也可。"
        "只返回JSON "
        '{"continuous":true,"complete":true,"observation":"具体的跨句画面依据"}。'
        + json.dumps(windows, ensure_ascii=False)
    )
    key = contract.digest([prompt, contract.file_sha256(media["sheet"]), media["before"]])[:24]
    raw = editor._call(prompt, directory / "camera_variety" / f"join_{key}.json", media["sheet"])
    if (
        raw.get("continuous") is True
        and raw.get("complete") is True
        and isinstance(raw.get("observation"), str)
        and raw["observation"].strip()
        and media["before"] == contract.source_identity(shot["video"])
    ):
        return {
            "indices": members,
            "key": key,
            "observation": raw["observation"],
            "source_identity": media["before"],
            "windows": [{"start": lo, "end": hi} for _, _, lo, hi in windows],
        }
    return None


def replacement(manifest, analysis, plan, timing, group, candidate, shot, directory, editor):
    indices = group["indices"]
    join = verify_join(group, candidate, shot, directory, editor) if len(indices) > 1 else None
    if len(indices) > 1 and join is None:
        return None
    cursor = candidate["start"]
    changes = {}
    for index in indices:
        sentence = plan["sentences"][index - 1]
        duration = group["durations"][index]
        end = cursor + duration + group["pad"]
        key = editor._review_proposed_window(
            manifest, plan, directory, {**sentence, "require_sentence_fit": True}, shot, (cursor, end)
        )
        if not key:
            return None
        changes[index] = {
            "shot_id": shot["shot_id"],
            "source_start_us": cursor,
            "sentence_fit_required": True,
            "window_review_key": key,
            "audio_duration_us": duration,
        }
        cursor = end
    rows = [
        {
            **r,
            "parts": [changes[r["index"]]],
            "reason": "换用已逐句复看的连续展示，减少本稿重复机位或展示动作。",
        }
        if r["index"] in changes
        else r
        for r in plan["rows"]
    ]
    # A replacement must not inherit a binding for the previous pixels.
    # Keep disjoint bindings, and refuse to split an existing one.
    members = set(indices)
    existing = plan.get("camera_continuous_groups", [])
    if any(members.intersection(g["indices"]) and not set(g["indices"]) <= members for g in existing):
        return None
    retained = [g for g in existing if not members.intersection(g["indices"])]
    trial_groups = retained + ([join] if join else [])
    trial = editor._assemble(
        manifest, analysis, {**plan, "rows": rows, "camera_continuous_groups": trial_groups},
        narration_timing=timing, verify_sources=True
    )
    for index, part in changes.items():
        cut = trial["segments"][index - 1]
        start = part["source_start_us"]
        if (
            cut.get("span_parts")
            or cut["video"] != shot["video"]
            or round(cut["source_start"] * 1e6) != start
            or round(cut["source_end"] * 1e6) != start + part["audio_duration_us"] + group["pad"]
        ):
            return None
    return rows, trial["segments"], join


def refine(manifest, analysis, plan, timing, inventory, directory):
    """Optional last selection pass. Commit only a whole, verified camera change."""
    from . import ai_editor as editor

    if not inventory or manifest.get("shot_notes_general"):
        return
    selected = {p["shot_id"] for r in plan["rows"] for p in r.get("parts", [])}
    sources = {s["video"] for s in analysis["shots"] if s["shot_id"] in selected}
    if not any(s["video"] not in sources for s in analysis["shots"]) or not any(
        s.get("kind") in {"hook", "product_intro"} and not s.get("selling_points")
        for s in plan["sentences"]
    ):
        return
    audit = {"changes": [], "retained": []}
    media_cache = {}

    def media(shot, dest):
        key = (shot["video"], shot["source_start"], shot["source_end"])
        if key not in media_cache:
            media_cache[key] = editor._review_media(shot, dest)
        return media_cache[key]

    plan["camera_variety"] = audit
    try:
        segments = editor._assemble(manifest, analysis, plan, narration_timing=timing, verify_sources=True)[
            "segments"
        ]
        layout = groups(segments, plan, manifest)
        original = editor.timing_contract.split_sentences(manifest)
        for group in layout:
            indices = group["indices"]
            group["texts"] = [segments[n - 1]["text"] for n in indices]
            group["durations"] = {n: segments[n - 1]["audio_duration_us"] for n in indices}
            group["pad"] = editor.timing_contract.us_of(manifest.get("tail_pad_s"), 0)
            group["duration"] = sum(group["durations"].values()) + len(indices) * group["pad"]
            group["editable"] &= not any(
                editor._row_has_presentation(plan, row) for row in plan["rows"] if row["index"] in indices
            )
            group["editable"] &= not any(
                original[n - 1].get("visual_requirements")
                or shot_notes.note_for(manifest, n, original[n - 1])
                for n in indices
            )
        pairs = find_repeats(layout, segments, directory, media, editor._call, audit=audit)
        audit["pairs"] = [
            {"first": layout[a]["indices"], "second": layout[b]["indices"], "kind": k} for a, b, k in pairs
        ]
        reviews = {r["shot_id"]: r for r in plan["display_reviews"] if r["verdict"] == "matched"}
        shots = {s["shot_id"]: s for s in analysis["shots"]}
        menus = {m["index"]: m["candidates"] for m in inventory}
        changed = set()
        attempted = set()
        comparisons = 0
        recalled = False
        # When A recurs at B and C, changing A can leave the B-to-C return.
        # Recheck those surviving pairs on their own pixels before lower-priority edits.
        pending = list(pairs)
        while pending:
            pair = pending.pop(0)
            a, b, kind = pair
            if a in changed or b in changed:
                # The original pair no longer exists. Only compare other surviving pairs.
                continue
            # Prefer changing the ordinary group responsible for the most returns.
            targets = sorted(
                (n for n in (b, a) if layout[n]["editable"] and n not in changed),
                key=lambda n: -sum(n in (x, y) for x, y, _ in pairs),
            )
            for target in targets:
                group = layout[target]
                used = {g["video"] for g in layout}
                indices = group["indices"]
                allowed = set.intersection(*[{c["shot_id"] for c in menus[n]} for n in indices])
                skip = {sid for sid, shot in shots.items() if shot["video"] in used or sid not in allowed}
                choices = list(options(group, menus[indices[0]], skip, reviews, manifest))
                anchors = [shots[segments[n - 1]["shot_id"]].get("description", "") for n in indices]
                if len(choices) < 2 and not recalled and kind == "camera":
                    recalled = True
                    extra = recall_displays(manifest, plan, group, shots, directory, editor, used, anchors)
                    if extra:
                        reviews = {
                            r["shot_id"]: r for r in plan["display_reviews"] if r["verdict"] == "matched"
                        }
                        inventory = editor._build_edit_inventory(manifest, analysis, plan, timing)
                        menus = {m["index"]: m["candidates"] for m in inventory}
                        choices.extend(
                            options(
                                group,
                                [c for c in menus[indices[0]] if c["shot_id"] in extra],
                                set(),
                                reviews,
                                manifest,
                            )
                        )
                choices = [c for c in choices if (target, c["shot_id"]) not in attempted]
                if not choices or comparisons >= 8:
                    continue
                choices = shortlist(group, choices, anchors, directory, editor._call)
                while choices and comparisons < 8:
                    comparisons += 1
                    candidate = compare(
                        layout, pair, target, choices, shots, directory, media, editor._call
                    )
                    if candidate is None:
                        break
                    choices.remove(candidate)
                    attempted.add((target, candidate["shot_id"]))
                    if changed and not fits_revised_layout(
                        layout,
                        changed,
                        target,
                        candidate,
                        shots[candidate["shot_id"]],
                        directory,
                        media,
                        editor._call,
                    ):
                        audit["retained"].append(
                            {
                                "lines": indices,
                                "shot_id": candidate["shot_id"],
                                "reason": "repeat_in_revised_layout",
                            }
                        )
                        continue
                    try:
                        result = replacement(
                            manifest,
                            analysis,
                            plan,
                            timing,
                            group,
                            candidate,
                            shots[candidate["shot_id"]],
                            directory,
                            editor,
                        )
                    except ValueError as exc:
                        if editor.failure_fields(str(exc)).get("failure_class") == "vision":
                            raise
                        audit["retained"].append({"lines": indices, "reason": str(exc)})
                        continue
                    if result is not None:
                        plan["rows"], segments, join = result
                        members = set(indices)
                        plan["camera_continuous_groups"] = [
                            g for g in plan.get("camera_continuous_groups", [])
                            if not members.intersection(g["indices"])]
                        if join:
                            plan["camera_continuous_groups"].append(join)
                        group.update(
                            video=shots[candidate["shot_id"]]["video"],
                            start=candidate["start"],
                            end=candidate["end"],
                        )
                        audit["changes"].append(
                            {"lines": indices, "shot_id": candidate["shot_id"], "kind": kind}
                        )
                        changed.add(target)
                        related = sorted(
                            {
                                y if x == target else x
                                for x, y, k in pairs
                                if k == "camera" and target in (x, y)
                            }
                        )
                        rechecks = [
                            (x, y, "camera")
                            for pos, x in enumerate(related)
                            for y in related[pos + 1 :]
                            if y > x + 1
                            and x not in changed
                            and y not in changed
                            and (layout[x]["editable"] or layout[y]["editable"])
                        ]
                        rechecks.sort(
                            key=lambda p: not (layout[p[0]]["editable"] and layout[p[1]]["editable"])
                        )
                        pending = rechecks + [p for p in pending if p not in rechecks]
                        break
                    audit["retained"].append(
                        {"lines": indices, "shot_id": candidate["shot_id"], "reason": "window_not_verified"}
                    )
                if target in changed:
                    break
        partial = audit.get("incomplete_batches") or audit.get("screened_pairs") != audit.get(
            "eligible_pairs"
        )
        audit.update(
            status="sampled" if partial else "reviewed",
            comparison_calls=comparisons,
            detection_call_limit=12,
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        audit.update(status="unavailable", reason=str(exc))
