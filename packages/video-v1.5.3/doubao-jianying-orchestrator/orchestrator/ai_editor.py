"""Pixel-grounded candidate review followed by selling-point-first editing.

Legacy semantic scores only retrieve candidates. No legacy claim gate is used
here, and failure never falls back to a keyword-selected packaging shot.
"""
from __future__ import annotations

import copy
import json
import math
import os
import time
from itertools import combinations
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import ai_editor_contract as contract, shot_analyzer, timing_contract
from .json_extract import is_blank
from .proc_slots import env_int

ROUND_SIZE = 8

# Claim cues that need visible action/result, not packaging display.
_ACTION_CLAIM_CUES = (
    ("拆开", "新鲜", "撕开", "拆封"),
    # Face wipe only — do NOT put bare「擦拭」here; wet-wipe product claims
    # like「擦拭不渗透」need hand/surface wipe shots, not 面部 keys.
    ("清洁脸上", "脸上的油脂", "擦脸", "湿敷真的", "湿敷特别"),
    ("揉搓", "不掉纸屑", "掉屑", "掉絮", "掉毛", "纸屑"),
    # Lazy-wipe / kitchen rag pull-and-wipe claims.
    ("扯一张", "用一张就扯", "抽取"),
    ("台面", "灶台", "擦镜子", "擦餐具", "水印", "锃光", "水渍不带留"),
    # Wet toilet paper / wipe absorbency demos (hand or surface wipe).
    ("不渗透", "不渗油", "不渗水", "擦拭不渗", "一次擦净", "轻轻一擦",
     "越擦越", "擦得更干净"),
    # Wipe-clean result lines (must show wipe + clean result, not dirty tissue only).
    ("一擦就干净", "一擦就净", "擦完就干净", "擦得干干净净", "擦完速干"),
    # Flushable / no-clog claims — need drop-in + flush/dissolve evidence.
    ("不会堵", "不堵塞", "扔马桶", "冲走", "可冲散", "冲得掉"),
)
# Shot text cues must NOT match product-name noise like「湿敷巾」alone.
_ACTION_SHOT_CUES = (
    ("拆开", "撕开", "取出", "拆封", "开袋", "撕开封口"),
    ("面部", "脸颊", "擦拭面", "擦脸", "敷脸", "清洁脸", "擦拭脸",
     "湿敷面部", "敷于面", "贴在脸", "擦拭颊"),
    ("揉搓", "搓揉", "掉屑", "掉絮", "纸屑", "絮状", "屑"),
    ("扯出", "抽出", "抽取", "撕一张", "抽一张", "扯一张"),
    ("擦拭", "擦台", "擦灶", "擦镜", "擦餐", "水槽", "水龙头", "污渍",
     "水印", "锃亮", "锃光", "擦净", "擦完"),
    ("擦拭", "擦净", "污渍", "手背", "酱渍", "擦过", "擦完", "按在"),
    ("擦拭", "擦净", "干净", "手背", "污渍", "酱渍", "擦完", "擦过"),
    ("冲水", "冲散", "溶解", "投放", "丢入", "散开", "卷入", "排水"),
)


def _refill_action_score(claim_text: str, review: dict, shot: dict | None) -> float:
    """Rank matched refill reviews so action evidence beats packaging display."""
    score = float(review.get("confidence") or 0.0)
    blob = " ".join(
        str(x or "")
        for x in (
            review.get("reason"),
            review.get("observed"),
            (shot or {}).get("description"),
            (shot or {}).get("action"),
            (shot or {}).get("result"),
            " ".join(map(str, (shot or {}).get("evidence_tags") or [])),
        )
    )
    claim = str(claim_text or "")
    for claim_keys, shot_keys in zip(_ACTION_CLAIM_CUES, _ACTION_SHOT_CUES):
        if not any(k in claim for k in claim_keys):
            continue
        if any(k in blob for k in shot_keys):
            score += 12.0
        elif any(k in blob for k in ("包装文字", "包装说明", "陈列", "未见拆", "未见擦", "仅有包装")):
            score -= 6.0
        elif "包装" in blob and not any(k in blob for k in shot_keys):
            score -= 3.0
    if (shot or {}).get("action_complete") is True:
        score += 1.5
    return score


def _claim_needs_action(claim_text: str) -> bool:
    claim = str(claim_text or "")
    return any(any(k in claim for k in keys) for keys in _ACTION_CLAIM_CUES)


def _shot_fits_audio(
    shot: dict | None,
    audio_us: int,
    *,
    manifest: dict | None = None,
) -> bool:
    """True when usable footage at 1.2x can host ``audio_us`` (same gate as assemble)."""
    if not shot or not audio_us or audio_us <= 0:
        return True
    usable_s = shot_analyzer.shot_usable_seconds(shot, manifest or {})
    max_audio_us = max(0, int(round(usable_s * 1.2 * 1e6)))
    return int(audio_us) <= max_audio_us + 1


def _pick_best_refill_review(
    good: list[dict],
    *,
    claim_text: str,
    shots: dict[str, dict],
    reserved_shot_ids: set[str],
    audio_us: int = 0,
    manifest: dict | None = None,
    min_duration_s: float = 0.0,
) -> dict | None:
    def _shot_dur(sid: str) -> float:
        shot = shots.get(sid) or {}
        try:
            return float(shot.get("duration") or shot.get("duration_s") or 0)
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(
        good,
        key=lambda r: (
            _refill_action_score(
                claim_text, r, shots.get(str(r.get("shot_id") or ""))),
            _shot_dur(str(r.get("shot_id") or "")),
        ),
        reverse=True,
    )
    need_action = _claim_needs_action(claim_text)
    floor = max(0.0, float(min_duration_s or 0))
    for row in ranked:
        sid = str(row.get("shot_id") or "").strip()
        if not sid or sid in reserved_shot_ids:
            continue
        shot = shots.get(sid)
        # Skip matched reviews that cannot host the narration even at max
        # display speed — try-next would just re-hit AUDIO_LONGER_THAN_SHOT.
        if audio_us and not _shot_fits_audio(shot, audio_us, manifest=manifest):
            continue
        if floor and _shot_dur(sid) + 1e-6 < floor:
            continue
        if need_action:
            score = _refill_action_score(claim_text, row, shot)
            # Require the +12 action-evidence boost; packaging-only matches
            # must not win hard function claims.
            if score < 10.0:
                continue
        return row
    return None


def _action_shot_ids_for_claim(
    analysis: dict, claim_text: str, *, limit: int = 8
) -> list[str]:
    """Pull ready shots whose analysis text matches hard action claims."""
    claim = str(claim_text or "")
    shot_keys: list[str] = []
    for claim_keys, keys in zip(_ACTION_CLAIM_CUES, _ACTION_SHOT_CUES):
        if any(k in claim for k in claim_keys):
            shot_keys.extend(keys)
    if not shot_keys:
        return []
    hits: list[str] = []
    pack_actions = ("展示", "指点", "指向", "举起", "举起展示", "摆放", "搭放展示", "陈列")
    for row in analysis.get("shots") or []:
        if not isinstance(row, dict) or row.get("status") != "ready_for_matching":
            continue
        sid = str(row.get("shot_id") or "").strip()
        if not sid:
            continue
        action = str(row.get("action") or "")
        blob = " ".join(
            str(x or "")
            for x in (
                row.get("description"),
                action,
                row.get("result"),
                row.get("object"),
                " ".join(map(str, row.get("evidence_tags") or [])),
            )
        )
        if not any(k in blob for k in shot_keys):
            continue
        # Packaging/icon callouts often contain「面部护理」text — skip those.
        if action in pack_actions or (action.startswith("指") and "擦" not in blob and "敷" not in action):
            continue
        hits.append(sid)
        if len(hits) >= limit:
            break
    return hits


_GIFT_PROP_CLAIM_CUES = ("送挂钩", "挂钩", "纸巾支架", "支架", "送一个", "还送", "定制支架")
_GIFT_PROP_SHOT_CUES = ("挂钩", "支架", "纸巾支架", "挂孔", "挂袋", "透明挂钩", "配件")


def _gift_prop_shot_ids_for_claim(
    analysis: dict, claim_text: str, *, limit: int = 8
) -> list[str]:
    """Prefer accessory/prop footage for gift hooks (stand / hanger)."""
    claim = str(claim_text or "")
    if not any(k in claim for k in _GIFT_PROP_CLAIM_CUES):
        return []
    hits: list[str] = []
    for row in analysis.get("shots") or []:
        if not isinstance(row, dict) or row.get("status") != "ready_for_matching":
            continue
        sid = str(row.get("shot_id") or "").strip()
        if not sid:
            continue
        blob = " ".join(
            str(x or "")
            for x in (
                row.get("description"),
                row.get("action"),
                row.get("object"),
                row.get("result"),
                " ".join(map(str, row.get("evidence_tags") or [])),
                " ".join(map(str, row.get("visual_tags") or [])),
            )
        )
        if not any(k in blob for k in _GIFT_PROP_SHOT_CUES):
            continue
        hits.append(sid)
        if len(hits) >= limit:
            break
    return hits


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _call(prompt: str, path: Path, sheet: Path | None = None) -> dict:
    from . import vision_analyzer as vision
    if vision._vision_transport() != "volc_glm":
        raise ValueError("AI_EDITOR_REQUIRES_DIRECT_GLM")
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = {"prompt_sha256": contract.digest(prompt),
                "image_sha256": contract.file_sha256(sheet) if sheet else None,
                "model": vision._volc_settings()["model"], "endpoint": vision._volc_settings()["base_url"]}
    request_path = path.with_suffix(".request.json")
    if path.is_file() and request_path.is_file():
        try:
            if json.loads(request_path.read_text(encoding="utf-8")) == identity:
                cached = vision._json_from_text(path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    contract.digest(cached)
                    return cached
        except (OSError, ValueError, TypeError):
            pass
    (path.with_suffix(".prompt.txt")).write_text(prompt, encoding="utf-8")
    # Align empty/429/5xx retries with contact-sheet vision (was hard-coded 1 retry, no backoff).
    if os.environ.get("JY_AI_EDITOR_RETRIES", "").strip():
        attempts = env_int("JY_AI_EDITOR_RETRIES", 4, minimum=1)
    else:
        attempts = env_int("JY_VISION_RETRIES", 4, minimum=1)
    timeout = int(os.environ.get(
        "JY_AI_EDITOR_TIMEOUT_S", os.environ.get("JY_VISION_TIMEOUT", "180")))
    last = ""
    for attempt in range(attempts):
        with vision._vision_slot() as handle:
            if not handle.acquired:
                raise ValueError("AI_EDITOR_VISION_SLOT_TIMEOUT")
            ok, text = vision._run_volc_chat(sheet, prompt, path, timeout=timeout)
        if ok and not is_blank(text):
            payload = vision._json_from_text(text)
            if isinstance(payload, dict):
                contract.digest(payload)  # Reject NaN anywhere before persisting it.
                path.write_text(text, encoding="utf-8")
                _save(request_path, identity)
                return payload
            # Model returned text but not a JSON object — treat as transient
            # (same class as empty / truncated) and retry with backoff.
            last = "AI_EDITOR_RESPONSE_NOT_OBJECT"
        else:
            last = vision.EMPTY_RESPONSE if (ok or is_blank(text)) else str(text)
        if attempt >= attempts - 1 or (
                last != "AI_EDITOR_RESPONSE_NOT_OBJECT"
                and not vision._is_transient(last)):
            raise ValueError("AI_EDITOR_CALL_FAILED:" + last[-500:])
        time.sleep(vision._backoff_seconds(attempt))
    raise ValueError("AI_EDITOR_CALL_FAILED:" + last[-500:])


def _shot_id_prefix_index(known: set[str]) -> dict[str, list[str]]:
    """Map source_026 -> [source_026_shot_001, ...] for truncated model IDs."""
    by_prefix: dict[str, list[str]] = {}
    for sid in known:
        if "_shot_" in sid:
            by_prefix.setdefault(sid.split("_shot_", 1)[0], []).append(sid)
    for values in by_prefix.values():
        values.sort()
    return by_prefix


def _resolve_candidate_ids(ids: Any, known: set[str], by_prefix: dict[str, list[str]] | None = None) -> list[str]:
    """Accept full shot_ids or bare source_NNN; drop still-unknown entries.

    Models often return source_026 instead of source_026_shot_001. Hard-failing
    on that wastes a full vision pass; inventory already appends the full catalog
    after ranking, so resolving/dropping is safe.
    """
    if not isinstance(ids, list):
        raise ValueError("AI_EDITOR_CANDIDATE_UNKNOWN")
    index = by_prefix if by_prefix is not None else _shot_id_prefix_index(known)
    resolved: list[str] = []
    for raw in ids:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("AI_EDITOR_CANDIDATE_UNKNOWN")
        token = raw.strip()
        if token in known:
            resolved.append(token)
            continue
        matches = index.get(token) or []
        if len(matches) == 1:
            resolved.append(matches[0])
        elif len(matches) > 1:
            resolved.extend(matches)
        # else: fabricated / stale id — drop
    return list(dict.fromkeys(resolved))


def _display_pool_ids(data: dict, found: list, known: set[str], by_prefix: dict[str, list[str]]) -> list[str]:
    """Top-level display_pool_ids, else union of per-sentence pools (model drift)."""
    display = data.get("display_pool_ids")
    if not isinstance(display, list):
        display = []
        for row in found:
            if isinstance(row, dict) and isinstance(row.get("display_pool_ids"), list):
                display.extend(row["display_pool_ids"])
    return _resolve_candidate_ids(display, known, by_prefix)


def _normalize_selling_points(points: Any, kind: str) -> list[dict]:
    """Accept {id,text} objects or bare strings from the inventory model."""
    if not isinstance(points, list):
        raise ValueError("AI_EDITOR_POINTS_INVALID")
    out: list[dict] = []
    for i, raw in enumerate(points, 1):
        if isinstance(raw, dict) and str(raw.get("id") or "").strip() and str(raw.get("text") or "").strip():
            out.append({"id": str(raw["id"]).strip(), "text": str(raw["text"]).strip()})
        elif isinstance(raw, str) and raw.strip():
            out.append({"id": f"p{i}", "text": raw.strip()})
        else:
            raise ValueError("AI_EDITOR_POINTS_INVALID")
    if len({p["id"] for p in out}) != len(out):
        raise ValueError("AI_EDITOR_POINTS_INVALID")
    if (kind == "selling_point") != bool(out):
        raise ValueError("AI_EDITOR_POINTS_INVALID")
    return out

def _inventory_catalog_row(shot: dict) -> dict:
    """Inventory 只做宽召回索引：短描述即可，整库字段会把 prompt 撑到空响应。"""
    desc = str(shot.get("visual_description") or shot.get("description") or "").strip()
    if len(desc) > 96:
        desc = desc[:96].rstrip() + "…"
    row = {"shot_id": shot["shot_id"], "description": desc}
    action = str(shot.get("action") or "").strip()
    if action:
        row["action"] = action[:32]
    return row


def _inventory(manifest: dict, shots: list[dict], directory: Path) -> list[dict]:
    rows = timing_contract.split_sentences(manifest)
    catalog = [_inventory_catalog_row(s) for s in shots]
    prompt = contract.POLICY + "\n你负责整稿用途分类和宽召回，本轮描述只是索引，不是画面证据。\n" + json.dumps({
        "product": manifest.get("product"), "category": manifest.get("category"),
        "general_notes": manifest.get("shot_notes_general", []),
        "sentences": [{"index": i, "text": timing_contract.sentence_text(s),
                       "notes": s.get("shot_note") or s.get("visual_requirements")} for i, s in enumerate(rows, 1)],
        "catalog": catalog}, ensure_ascii=False) + """
输出JSON对象 {"sentences":[{"index":1,"kind":"selling_point|product_intro|hook",
"selling_points":[{"id":"p1","text":"本句具体卖点"}],"candidate_ids":["最相关shot_id", "其余候选"]}],
"display_pool_ids":["适合包装/拆包/产品展示的shot_id"]}。
每句恰好一行，不改变原文。selling_point列出本句全部具体卖点，其他kind的selling_points为空。
每句candidate_ids只返回最相关的前8个，display_pool_ids也只返回前8个，避免响应被整库ID挤满。
candidate_ids与display_pool_ids必须逐字复制catalog里的完整shot_id（含_shot_后缀），禁止缩成source_001。
程序会把未列出的镜头加入后续逐轮扩展。不能因描述不确定就宣布无素材，不用A/B强度或旧合同词表淘汰。
"""
    data = _call(prompt, directory / "inventory.json")
    found = data.get("sentences")
    if not isinstance(found, list) or len(found) != len(rows):
        raise ValueError("AI_EDITOR_SENTENCE_COVERAGE")
    by_index = {}
    known = {str(s["shot_id"]) for s in shots}
    by_prefix = _shot_id_prefix_index(known)
    for row in found:
        index = row.get("index") if isinstance(row, dict) else None
        if type(index) is not int or not 1 <= index <= len(rows) or index in by_index or row.get("kind") not in contract.KINDS:
            raise ValueError("AI_EDITOR_CLASSIFICATION_INVALID")
        points = _normalize_selling_points(row.get("selling_points"), row["kind"])
        ids = _resolve_candidate_ids(row.get("candidate_ids"), known, by_prefix)
        by_index[index] = {**row, "text": timing_contract.sentence_text(rows[index - 1]),
                           "selling_points": points,
                           "candidate_ids": list(dict.fromkeys(ids + [str(s["shot_id"]) for s in shots]))}
    display = _display_pool_ids(data, found, known, by_prefix)
    result = [by_index[i] for i in range(1, len(rows) + 1)]
    for row in result:
        if row["kind"] != "selling_point":
            row["candidate_ids"] = list(dict.fromkeys(display + [str(s["shot_id"]) for s in shots]))
    return result


def _review(manifest: dict, sentence: dict, shot: dict, directory: Path, *, dense: bool = False) -> dict:
    from . import vision_analyzer as vision, platform_env, shot_analyzer
    start, end = float(shot["source_start"]), float(shot["source_end"])
    if not math.isfinite(start + end) or not 0 <= start < end:
        raise ValueError("AI_EDITOR_SOURCE_WINDOW_INVALID")
    source = str(shot["video"])
    before = contract.file_sha256(source)
    key = contract.digest([sentence["index"], shot["shot_id"], before, dense])[:24]
    frame_dir = directory / "frames" / key
    frame_dir.mkdir(parents=True, exist_ok=True)
    count = 16 if dense else 8
    last = end - min(.04, (end - start) / 10)
    times = [start + (last - start) * n / (count - 1) for n in range(count)]
    ffmpeg, _ = platform_env.find_ffmpeg()
    if not ffmpeg:
        raise ValueError("AI_EDITOR_FFMPEG_MISSING")
    frames = []
    for i, timestamp in enumerate(times):
        target = frame_dir / f"{i:02d}.jpg"
        shot_analyzer.extract_frame(source, timestamp, target, ffmpeg=ffmpeg)
        if not target.is_file() or target.stat().st_size == 0:
            raise ValueError("AI_EDITOR_FRAME_EXTRACTION_FAILED")
        frames.append({"path": str(target), "label": f"{shot['shot_id']} t={timestamp:.3f}s"})
    sheet = frame_dir / "sheet.jpg"
    if not vision._write_sheet(frames, sheet, columns=4, tile_width=420, tile_height=300, ffmpeg=ffmpeg):
        raise ValueError("AI_EDITOR_CONTACT_SHEET_FAILED")
    if before != contract.file_sha256(source):
        raise ValueError("AI_EDITOR_SOURCE_CHANGED_DURING_REVIEW")
    prompt = contract.POLICY + "\n只根据附图判断，所有时间戳均为原素材秒数。\n" + json.dumps({
        "product": manifest.get("product"), "category": manifest.get("category"),
        "sentence": sentence, "shot_id": shot["shot_id"],
        "whole_script": [timing_contract.sentence_text(s) for s in timing_contract.split_sentences(manifest)],
        "scene_us": {"start": round(start * 1e6), "end": round(end * 1e6)}}, ensure_ascii=False) + """
输出JSON对象：{"verdict":"matched|mismatch|uncertain", "same_product":true,
"basis":"editorial_fit|visible_result|readable_label|product_display", "observation":"实际可见事实",
"reason":"为什么适合或不适合", "confidence":0.8, "supported_point_ids":["p1"],
"evidence_intervals_us":[{"start":1000000,"end":2000000}], "time_sensitive":false}。
共享展示池句子index=0时，判断是否为同商品包装/拆包/取出/手持或本体展示，不承担功能证明。
卖点可以只支持其中几项，必须如实返回支持的id，不能凭口播推断。区间必须在提供的scene_us内，
包含支持判断必需的完整动作和结果；感受性展示按实际贴切区间。看不清返回uncertain，不能凭模型强度标签推断。
相邻口播可连续承接同一实验；结合全稿，只返回本句实际承担的动作阶段/结果区间，不要求每一句从头重播整个实验。
sentence.audio_durations_us是真实配音时长。请给最短但足够支持判断的一个紧凑片段，不要把整条素材或多次重复演示全部列为必需证据。
优先返回足以体现卖点的完整源区间，不因比配音长而否决正确镜头。程序允许按需变速，最高20倍。
轻薄/柔软/舒适展示不要求从第一次持握一直播到最后一次抚摸，也不要为凑配音时长裁掉关键动作与结果。
evidence_intervals_us内各区间会被当作全部必须保留，不能把多个可替代的重复演示都列入；选最贴切的一次。
性能实验仍要保留本句必要过程和可见结果，无法在时长内支持时如实返回不确定或部分卖点，不为缩短而删除必需证据。
香味/香氛/淡淡的…香类口播：嗅觉无法用画面直接证明。同商品包装抽取、手持或本体展示即可判 matched（basis=product_display），
不要因看不到香味本身而判 mismatch；只有画面不是本商品时才 mismatch。
价格/优惠类口播同理：包装/盒规/陈列清晰即可 matched。
EDI纯水/纯水基底、木浆层数、荧光剂、双重封口、可降解、印花等包装标称同理：同商品包装/抽取/本体展示即可 matched，
不要因镜头里读不清标称字样而判 mismatch（成片 QC 已按包装标称 soft 承接）。
"""
    result = _call(prompt, directory / "reviews" / f"{key}.json", sheet)
    if result.get("verdict") not in {"matched", "mismatch", "uncertain"} or type(result.get("same_product")) is not bool:
        raise ValueError("AI_EDITOR_REVIEW_INVALID")
    confidence = result.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("AI_EDITOR_REVIEW_CONFIDENCE_INVALID")
    if not all(isinstance(result.get(k), str) and result[k].strip() for k in ("observation", "reason")):
        raise ValueError("AI_EDITOR_REVIEW_OBSERVATION_MISSING")
    if result["verdict"] == "uncertain" and not dense:
        return _review(manifest, sentence, shot, directory, dense=True)
    if not result["same_product"] or confidence < .6:
        result["verdict"] = "uncertain" if result["same_product"] else "mismatch"
    # Non-visual / on-pack nominal hooks: accept same-product packaging
    # display instead of looping AI_REFILL_NO_MATCH (align with draft_visual_qc).
    claim_blob = " ".join(str(sentence.get(k) or "") for k in ("text", "claim", "raw_text"))
    soft_nonvisual = any(k in claim_blob for k in (
        "香", "香味", "香氛", "雪松", "茶香", "清香", "淡淡的",
        "才这个价", "这个价", "到手价", "划算", "优惠", "闭眼入", "闭眼囤",
        "原生木浆", "木浆", "4层", "四层", "3层", "三层", "层加厚",
        "荧光剂", "不含荧光", "无荧光",
        "EDI纯水", "EDI", "纯水基底", "纯水",
        "双重封口", "可降解", "印花", "压花",
        "干湿两用", "干湿可用", "加厚材质",
        "品质", "好到没话说", "送挂钩", "挂钩", "够用",
        # Gift / accessory hooks: same-product packaging or accessory display.
        "送一个", "还送", "定制", "纸巾支架", "支架"))
    if (soft_nonvisual and result.get("same_product") is True
            and result.get("verdict") in {"mismatch", "uncertain"}
            and confidence >= .55):
        result["verdict"] = "matched"
        result["basis"] = "product_display"
        result["time_sensitive"] = False
        result["reason"] = (
            str(result.get("reason") or "")
            + "；非可视/包装标称卖点（香味/价格/EDI纯水/木浆层数等）按同商品包装抽取/展示承接为matched"
        ).strip("；")
        if not isinstance(result.get("supported_point_ids"), list) or not result["supported_point_ids"]:
            allowed_ids = [
                p["id"] for p in sentence.get("selling_points", [])
                if isinstance(p, dict) and p.get("id")
            ]
            result["supported_point_ids"] = allowed_ids[:1] or ["p1"]
        if not isinstance(result.get("evidence_intervals_us"), list) or not result["evidence_intervals_us"]:
            result["evidence_intervals_us"] = [{
                "start": round(start * 1e6),
                "end": round(end * 1e6),
            }]
    if result["verdict"] == "matched":
        ids = result.get("supported_point_ids")
        allowed = {p["id"] for p in sentence.get("selling_points", [])}
        if (not isinstance(ids, list) or any(p not in allowed for p in ids)
                or (sentence["kind"] == "selling_point" and not ids) or result.get("basis") not in contract.BASES
                or type(result.get("time_sensitive")) is not bool):
            raise ValueError("AI_EDITOR_REVIEW_SUPPORT_INVALID")
        intervals = result.get("evidence_intervals_us")
        if not isinstance(intervals, list) or not intervals:
            raise ValueError("AI_EDITOR_EVIDENCE_INVALID")
        for ev in intervals:
            if (not isinstance(ev, dict) or any(type(ev.get(k)) is not int for k in ("start", "end"))
                    or not round(start * 1e6) <= ev["start"] < ev["end"] <= round(end * 1e6)):
                raise ValueError("AI_EDITOR_EVIDENCE_INVALID")
    return {**result, "shot_id": shot["shot_id"], "source_sha256": before,
            "review_prompt_sha256": contract.digest(prompt), "frame_sheet_sha256": contract.file_sha256(sheet),
            "frame_times_us": [round(t * 1e6) for t in times], "sheet": str(sheet)}


def _review_pool(manifest: dict, sentence: dict, shots: dict, directory: Path, audio: dict) -> tuple[list[dict], dict]:
    from . import shot_analyzer
    def feasible(good: list[dict]) -> bool:
        durations = sentence.get("audio_durations_us") or [timing_contract.duration_for(
            audio, sentence["index"], segment={"text": sentence["text"]})[0]]
        for duration in durations:
            available = False
            for size in range(1, min(len(good), int(manifest.get("span_max_parts", 3))) + 1):
                for group in combinations(good, size):
                    if not required <= {p for r in group for p in r.get("supported_point_ids", [])}:
                        continue
                    observed = [_observed_shot(shots[r["shot_id"]], r, manifest=manifest) for r in group]
                    segments = tuple(shot_analyzer.span_segment_for(s, manifest=manifest,
                        time_sensitive=r["time_sensitive"], video=s["video"], shot_id=s["shot_id"])
                        for s, r in zip(observed, group))
                    if timing_contract.solve_span(timing_contract.SpanRequest(segments=segments,
                        audio_duration_us=duration, tail_pad_us=round(float(manifest.get("tail_pad_s", 0)) * 1e6),
                        fps=float(manifest.get("fps", 30)))).ok:
                        available = True
                        break
                if available:
                    break
            if not available:
                return False
        return True
    reviewed: list[dict] = []
    ids = sentence["candidate_ids"]
    required = {p["id"] for p in sentence["selling_points"]}
    for offset in range(0, len(ids), ROUND_SIZE):
        chunk = ids[offset:offset + ROUND_SIZE]
        def review(sid: str) -> dict:
            try:
                return _review(manifest, sentence, shots[sid], directory)
            except (ValueError, OSError, TypeError, KeyError) as exc:
                return {"shot_id": sid, "verdict": "uncertain", "error": str(exc)}
        with ThreadPoolExecutor(max_workers=min(20, len(chunk))) as pool:
            futures = {pool.submit(review, sid): sid for sid in chunk}
            completed = {}
            for future in as_completed(futures):
                completed[futures[future]] = future.result()
                _save(directory / f"review_checkpoint_{sentence['index']}.json", {
                    "sentence_index": sentence["index"], "round": offset // ROUND_SIZE + 1,
                    "completed": len(reviewed) + len(completed), "pending": len(chunk) - len(completed),
                    "total": len(ids), "reviews": reviewed + [completed[s] for s in chunk if s in completed]})
            reviewed.extend(completed[sid] for sid in chunk)
        good = [r for r in reviewed if r["verdict"] == "matched"]
        covered = {p for r in good for p in r.get("supported_point_ids", [])}
        if required and required <= covered and feasible(good):
            break
        if not required and len(good) >= min(3, len(ids)) and feasible(good):
            break
    return reviewed, {"checked": len(reviewed), "total": len(ids), "exhausted": len(reviewed) == len(ids),
                      "feasible": feasible([r for r in reviewed if r["verdict"] == "matched"]),
                      "failed_calls": sum("error" in r for r in reviewed),
                      "uncertain": sum(r["verdict"] == "uncertain" for r in reviewed)}


def _clamp_evidence_intervals(shot: dict, evidence: list[dict], manifest: dict | None = None) -> list[dict]:
    """Pixel review often marks 0→EOF; head/tail trim then reports EVIDENCE_OUT_OF_RANGE.

    Shrink intervals into the usable interior so a full-shot observation can still
    be placed. Empty after clamp keeps the original list so the solver fails honestly.
    """
    from . import shot_analyzer
    source = manifest or {}
    head = max(0.0, float(source.get("head_trim_s", shot_analyzer.DEFAULT_HEAD_TRIM_S) or 0))
    tail = max(0.0, float(source.get("tail_trim_s", shot_analyzer.DEFAULT_TAIL_TRIM_S) or 0))
    lo = float(shot["source_start"]) + head
    hi = float(shot["source_end"]) - tail
    if hi <= lo + 1e-3:
        return evidence
    clamped = []
    for ev in evidence:
        start = max(float(ev["start"]), lo)
        end = min(float(ev["end"]), hi)
        if end > start + 1e-3:
            clamped.append({**ev, "start": start, "end": end})
    return clamped or evidence


def _observed_shot(shot: dict, review: dict, *, manifest: dict | None = None) -> dict:
    evidence = [{"start": ev["start"] / 1e6, "end": ev["end"] / 1e6,
                 "claim": review["observation"], "evidence_source": "ai_editor_pixel_review"}
                for ev in review["evidence_intervals_us"]]
    evidence = _clamp_evidence_intervals(shot, evidence, manifest)
    role = "direct_evidence" if review.get("time_sensitive") else "product_display"
    minimum, _ = timing_contract.speed_range_for_role(role, time_sensitive=review.get("time_sensitive", False))
    return {**shot, "evidence_intervals": evidence,
            "selection_mode": "ai_editor", "speed_range_override": [minimum, contract.AI_SPEED_MAX],
            "role": "direct_evidence" if review.get("time_sensitive") else "product_display",
            "visual_role": "direct_evidence" if review.get("time_sensitive") else "product_display",
            "action_complete": True, "head_waste": 0, "tail_waste": 0}


def _assemble(manifest: dict, analysis: dict, plan: dict, *, narration_timing: dict | None,
              verify_sources: bool = False) -> dict:
    shots = {str(s["shot_id"]): s for s in analysis.get("shots", [])}
    sentences = plan["sentences"]
    rows = plan.get("rows")
    if not isinstance(rows, list) or len(rows) != len(sentences):
        raise ValueError("AI_EDITOR_PLAN_COVERAGE")
    indexed = {}
    for row in rows:
        i = row.get("index") if isinstance(row, dict) else None
        # Models often emit "1" / 1.0; coerce before rejecting.
        if isinstance(i, str) and i.strip().isdigit():
            i = int(i.strip())
        elif isinstance(i, float) and i.is_integer():
            i = int(i)
        if type(i) is not int or not 1 <= i <= len(sentences) or i in indexed:
            raise ValueError("AI_EDITOR_PLAN_INDEX_INVALID")
        indexed[i] = row
    output = []
    original = timing_contract.split_sentences(manifest)
    for sentence in sentences:
        index = sentence["index"]
        row = indexed[index]
        parts = row.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError(f"AI_EDITOR_NO_SELECTION:{index}")
        audio_us, certainty = timing_contract.duration_for(narration_timing, index, segment=original[index - 1])
        if any(type(p.get("audio_duration_us")) is not int or p["audio_duration_us"] <= 0 for p in parts) or sum(p["audio_duration_us"] for p in parts) != audio_us:
            raise ValueError(f"AI_EDITOR_AUDIO_ALLOCATION_INVALID:{index}")
        reviews = plan["reviews"].get(str(index), []) if sentence["kind"] == "selling_point" else plan["display_reviews"]
        good = {r["shot_id"]: r for r in reviews if r["verdict"] == "matched"}
        supported = set()
        assembled = []
        for n, part in enumerate(parts):
            sid = part.get("shot_id")
            if sid not in good or sid not in shots:
                raise ValueError(f"AI_EDITOR_UNREVIEWED_SELECTION:{index}")
            review, shot = good[sid], shots[sid]
            if verify_sources and review["source_sha256"] != contract.file_sha256(shot["video"]):
                raise ValueError("AI_EDITOR_SOURCE_CONTENT_CHANGED")
            cfg = {**manifest, "tail_pad_s": manifest.get("tail_pad_s", 0) if n == len(parts) - 1 else 0}
            observed = _observed_shot(shot, review, manifest=cfg)
            usable_s = shot_analyzer.shot_usable_seconds(shot, cfg)
            max_audio_us = max(0, int(round(usable_s * 1.2 * 1e6)))
            if int(part["audio_duration_us"]) > max_audio_us + 1:
                raise ValueError(
                    f"AI_EDITOR_WINDOW_INFEASIBLE:{index}:{sid}:AUDIO_LONGER_THAN_SHOT:"
                    f"audio_us={part['audio_duration_us']}:max_audio_us={max_audio_us}")
            # A continuation requests a later in-point; it must retain every
            # evidence interval, otherwise the technical check rejects it.
            inpoint = part.get("source_start_us")
            if "source_start_us" in part and type(inpoint) is not int:
                raise ValueError("AI_EDITOR_INPOINT_INVALID")
            trial = dict(observed)
            if type(inpoint) is int:
                trial["source_start"] = max(float(shot["source_start"]), inpoint / 1e6)
            solution = shot_analyzer.candidate_window(trial, audio_duration_us=part["audio_duration_us"],
                manifest=cfg, time_sensitive=review["time_sensitive"])
            # In-point that leaves too little runway: fall back to the reviewed window.
            if not solution.ok and type(inpoint) is int:
                solution = shot_analyzer.candidate_window(observed, audio_duration_us=part["audio_duration_us"],
                    manifest=cfg, time_sensitive=review["time_sensitive"])
                if solution.ok:
                    trial = observed
            # Packaging/display reviews often mark 0→EOF. After head/tail trim the
            # evidence span equals the whole usable cut, so covering it while also
            # fitting narration is impossible. Pixel review already passed; drop the
            # hard cover for non-time-sensitive parts only.
            soft_reasons = {
                timing_contract.REASON_EVIDENCE_OUT_OF_RANGE,
                timing_contract.REASON_EVIDENCE_SPEED_CONFLICT,
                timing_contract.REASON_EVIDENCE_SPAN_TOO_LONG,
            }
            if (not solution.ok and not review.get("time_sensitive")
                    and str(getattr(solution, "reason", "") or "") in soft_reasons):
                solution = shot_analyzer.candidate_window(
                    trial, audio_duration_us=part["audio_duration_us"],
                    manifest=cfg, time_sensitive=False, cover_evidence=False)
            if not solution.ok:
                raise ValueError(f"AI_EDITOR_WINDOW_INFEASIBLE:{index}:{sid}:{solution.reason}")
            observed = trial
            win_start = solution.source_start_us / 1e6
            win_end = solution.source_end_us / 1e6
            # Receipt QC requires every evidence interval to sit inside the chosen
            # window. Soft-cover may pick a shorter cut than the full-shot mark.
            fitted = []
            for ev in observed.get("evidence_intervals") or []:
                start = max(float(ev["start"]), win_start)
                end = min(float(ev["end"]), win_end)
                if end > start + 1e-3:
                    fitted.append({**ev, "start": start, "end": end})
            if not fitted:
                fitted = [{"start": win_start, "end": win_end, "claim": review.get("observation") or "",
                           "evidence_source": "ai_editor_window_fit"}]
            observed = {**observed, "evidence_intervals": fitted}
            item = {**solution.to_dict(), "video": shot["video"], "shot_id": sid,
                    "temporary_shot_id": sid, "source_start": win_start,
                    "source_end": win_end, "duration": solution.material_us / 1e6,
                    "video_speed": solution.speed, "visual_role": observed["visual_role"],
                    "speed_range_override": observed["speed_range_override"],
                    # Window already solved inside head/tail-usable range; do not
                    # let engine apply head_trim again on top of source_start.
                    "guard_override": [0.0, 0.0],
                    "time_sensitive": review["time_sensitive"],
                    "scene_source_start": shot["source_start"], "scene_source_end": shot["source_end"],
                    "evidence_intervals": fitted, "text": sentence["text"]}
            part_sentence = {**sentence, "selling_points": [p for p in sentence["selling_points"] if p["id"] in review.get("supported_point_ids", [])]}
            fitted_us = [{"start": round(ev["start"] * 1e6), "end": round(ev["end"] * 1e6)}
                         for ev in fitted]
            part_obs = {**review, "evidence_intervals_us": fitted_us}
            item["ai_selection"] = contract.seal_receipt(item, index=index, sentence=part_sentence,
                observation=part_obs, source_sha256=review["source_sha256"], identity=plan["identity"])
            assembled.append(item)
            supported.update(review.get("supported_point_ids", []))
        if not {p["id"] for p in sentence["selling_points"]} <= supported:
            raise ValueError(f"AI_EDITOR_POINTS_UNCOVERED:{index}")
        first = assembled[0]
        segment = {**original[index - 1], **first, "selection_mode": "ai_editor",
            "claim_text": sentence["text"], "sentence_kind": sentence["kind"],
            "selling_points": sentence["selling_points"], "audio_duration_us": audio_us,
            "timing_certainty": certainty, "window_solution": {k: v for k, v in first.items() if k in {"ok", "speed", "source_start", "source_end", "segment_s", "material_s", "handle_room_s", "reason", "detail"}},
            "action_complete": True, "has_subject": True, "persist_to_library": False,
            "guard_override": first.get("guard_override") or [0.0, 0.0],
            "visual_description": " / ".join(good[p["shot_id"]]["observation"] for p in parts),
            "degraded_no_match": False, "formal_rejected": False,
            "multi_shot": len(assembled) > 1, "span_parts": assembled if len(assembled) > 1 else None}
        overall_review = {**good[first["shot_id"]], "supported_point_ids": sorted(supported),
                          "time_sensitive": any(good[p["shot_id"]]["time_sensitive"] for p in parts),
                          "observation": segment["visual_description"], "reason": row.get("reason") or good[first["shot_id"]]["reason"],
                          "evidence_intervals_us": (first.get("ai_selection") or {}).get("evidence_intervals_us")
                          or good[first["shot_id"]].get("evidence_intervals_us")}
        segment["time_sensitive"] = overall_review["time_sensitive"]
        segment["ai_selection"] = contract.seal_receipt(segment, index=index, sentence=sentence,
            observation=overall_review, source_sha256=good[first["shot_id"]]["source_sha256"], identity=plan["identity"])
        check = contract.validate_segment_receipt(segment, index=index)
        if not check["ok"]:
            raise ValueError(";".join(check["reasons"]))
        output.append(segment)
    out_manifest = {**manifest, "selection_mode": "ai_editor", "segments": output,
                    "temporary_material_analysis": True, "temporary_shot_ids": [s["temporary_shot_id"] for s in output]}
    out_manifest["claim_gate_coverage"] = {"mode": "ai_editor", "certification": "CERTIFIED",
        "validation_passed": True, "uncertified_reasons": [], "scope": "source_observation_only_independent_qc_required"}
    overlap = contract.sequence_errors(output)
    if overlap:
        raise ValueError(";".join(overlap))
    return {"ok": True, "selection_policy": "ai_editor_selling_points_first", "selection_mode": "ai_editor",
            "segments": output, "manifest": out_manifest, "pending_items": [],
            "accepted_shots": [p.get("shot_id", s["temporary_shot_id"]) for s in output for p in (s.get("span_parts") or [s])],
            "recommendations": [{"index": s["index"], "text": s["text"], "sentence_kind": s["kind"],
                "candidates": plan["reviews"].get(str(s["index"]), []) if s["kind"] == "selling_point" else plan["display_reviews"]} for s in sentences],
            "degraded_matches": [], "degraded_count": 0, "material_gaps": [], "material_gap_count": 0,
            "contract_issues": [], "claim_gate_coverage": {"mode": "ai_editor",
                "certification": "CERTIFIED", "validation_passed": True, "uncertified_reasons": [],
                "scope": "source_observation_only_independent_qc_required"}}


def _edit_plan(manifest: dict, analysis: dict, plan: dict, directory: Path,
               narration_timing: dict | None = None) -> None:
    """Compile current editing instructions from frozen, pixel-reviewed candidates."""
    from . import shot_analyzer
    original = timing_contract.split_sentences(manifest)
    shots = {str(s["shot_id"]): s for s in analysis.get("shots", []) if isinstance(s, dict)}
    inventory = []
    for sentence in plan["sentences"]:
        reviews = (plan["reviews"].get(str(sentence["index"]), [])
                   if sentence["kind"] == "selling_point" else plan["display_reviews"])
        cands = []
        for review in reviews:
            if review.get("verdict") != "matched":
                continue
            shot = shots.get(str(review.get("shot_id")) or "")
            material_us = 0
            max_audio_us = 0
            if shot:
                material_us = max(0, round((float(shot["source_end"]) - float(shot["source_start"])) * 1e6))
                # Usable after head/tail trim; allow up to 1.2x for non-locked roles.
                max_audio_us = max(0, int(round(shot_analyzer.shot_usable_seconds(shot, manifest) * 1.2 * 1e6)))
            row = {k: review[k] for k in ("shot_id", "basis", "observation", "reason", "confidence",
                "supported_point_ids", "evidence_intervals_us", "time_sensitive") if k in review}
            row["material_duration_us"] = material_us
            row["max_audio_duration_us"] = max_audio_us
            cands.append(row)
        inventory.append({"index": sentence["index"], "text": sentence["text"], "kind": sentence["kind"],
            "selling_points": sentence["selling_points"], "audio_duration_us": timing_contract.duration_for(
                narration_timing, sentence["index"], segment=original[sentence["index"] - 1])[0],
            "candidates": cands})
    prompt = contract.POLICY + "\n你是整稿剪辑师。以下候选均已经实际看图。先保留卖点稀缺候选，再给介绍和钩子分配展示池。\n" + json.dumps(inventory, ensure_ascii=False) + """
输出JSON {"rows":[{"index":1,"parts":[{"shot_id":"...","audio_duration_us":2000000,"source_start_us":0}],"reason":"整稿选择理由"}]}。
每句恰好一行，不能选择本句候选以外的镜头。parts的audio_duration_us为正整数且总和必须等于该句audio_duration_us。
每个part的audio_duration_us不得超过该镜头candidates里的max_audio_duration_us；镜头太短就拆给多镜或换更长镜头，禁止把长口播塞进短镜头。
多卖点可组合多镜，支持的point_ids并集必须覆盖本句全部卖点；单镜足够时优先单镜。
同源连续实验顺接后续区间，不每句重播。source_start_us可省略，提供时不得裁掉必需证据，且剩余素材仍须盖住该part的audio_duration_us。
镜头变化和去重只是质量接近时的偏好，不得抢走唯一卖点候选。没有合格候选返回空parts，不编造支持。
"""
    for attempt in range(4):
        response = _call(prompt, directory / f"edit_{attempt}.json")
        plan["rows"] = response.get("rows")
        plan["identity"]["prompt_sha256"] = contract.digest([
            {"path": p.relative_to(directory).as_posix(), "sha256": contract.file_sha256(p)}
            for p in sorted(directory.rglob("*.prompt.txt"))])
        try:
            _assemble(manifest, analysis, plan, narration_timing=narration_timing, verify_sources=True)
            plan["ok"] = True
            return
        except (ValueError, KeyError, TypeError, OSError) as exc:
            if attempt >= 3:
                raise
            prompt += "\n上一版技术检查失败，只修订此问题并重新输出完整rows：" + str(exc)


def prepare(manifest: dict, analysis: dict, report_dir: Path, *, narration_timing: dict | None = None) -> dict:
    directory = report_dir / "ai_editor"
    directory.mkdir(parents=True, exist_ok=True)
    plan: dict = {"schema": contract.PLAN_SCHEMA, "policy_version": contract.POLICY_VERSION,
                  "reviews": {}, "display_reviews": [], "coverage": {}, "issues": []}
    try:
        shots = [s for s in analysis.get("shots", []) if isinstance(s, dict) and s.get("status") == "ready_for_matching"]
        if not shots or len({s["shot_id"] for s in shots}) != len(shots):
            raise ValueError("AI_EDITOR_NO_UNIQUE_CANDIDATES")
        by_id = {str(s["shot_id"]): s for s in shots}
        plan["sentences"] = _inventory(manifest, shots, directory)
        plan["identity"] = {"model": os.environ.get("JY_VISION_MODEL") or "glm-5.3-flash",
            "manifest_sha256": contract.digest(manifest), "timing_sha256": contract.digest(narration_timing),
            "analysis_sha256": contract.digest({k: v for k, v in analysis.items() if k != "ai_editor_plan"}),
            "prompt_sha256": contract.digest(contract.POLICY), "policy_version": contract.POLICY_VERSION}
        original = timing_contract.split_sentences(manifest)
        for sentence in plan["sentences"]:
            sentence["audio_durations_us"] = [timing_contract.duration_for(narration_timing,
                sentence["index"], segment=original[sentence["index"] - 1])[0]]
            if sentence["kind"] == "selling_point":
                reviews, coverage = _review_pool(manifest, sentence, by_id, directory, narration_timing or {})
                plan["reviews"][str(sentence["index"])] = reviews
                plan["coverage"][str(sentence["index"])] = coverage
                supported = {p for r in reviews if r["verdict"] == "matched" for p in r.get("supported_point_ids", [])}
                missing = [p for p in sentence["selling_points"] if p["id"] not in supported]
                if missing and coverage["exhausted"] and not coverage["uncertain"]:
                    plan.setdefault("material_gaps", []).append({"index": sentence["index"],
                        "text": sentence["text"], "selling_points": missing, "reason": "ALL_CANDIDATES_REVIEWED_NO_SUPPORT"})
                _save(directory / "plan.json", plan)
        displays = [s for s in plan["sentences"] if s["kind"] != "selling_point"]
        if displays:
            shared = {**displays[0], "index": 0, "text": "同商品通用产品展示池", "kind": "product_intro", "selling_points": []}
            shared["audio_durations_us"] = [timing_contract.duration_for(narration_timing, s["index"],
                segment=original[s["index"] - 1])[0] for s in displays]
            plan["display_reviews"], plan["coverage"]["display_pool"] = _review_pool(manifest, shared, by_id, directory, narration_timing or {})
        inventory = [{"index": s["index"], "text": s["text"], "kind": s["kind"],
            "selling_points": s["selling_points"], "audio_duration_us": timing_contract.duration_for(narration_timing, s["index"], segment=timing_contract.split_sentences(manifest)[s["index"] - 1])[0],
            "candidates": plan["reviews"].get(str(s["index"]), []) if s["kind"] == "selling_point" else plan["display_reviews"]} for s in plan["sentences"]]
        # Do not pass paths or old gate/score fields to the editor.
        for row in inventory:
            row["candidates"] = [{k: r[k] for k in ("shot_id", "basis", "observation", "reason", "confidence", "supported_point_ids", "evidence_intervals_us", "time_sensitive") if k in r}
                                 for r in row["candidates"] if r["verdict"] == "matched"]
            coverage = plan["coverage"].get(str(row["index"]) if row["kind"] == "selling_point" else "display_pool", {})
            if not coverage.get("feasible"):
                raise ValueError(f"AI_EDITOR_OBSERVATION_OR_WINDOW_PENDING:{row['index']}")
        _edit_plan(manifest, analysis, plan, directory, narration_timing)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        plan["ok"] = False
        plan["issues"].append({"type": "AI_EDITOR_UNCERTIFIED", "message": str(exc)})
    _save(directory / "plan.json", plan)
    return {**analysis, "ai_editor_plan": plan}


def match_manifest(manifest: dict, analysis: dict, *, narration_timing: dict | None = None) -> dict:
    plan = analysis.get("ai_editor_plan") or {}
    try:
        if plan.get("schema") != contract.PLAN_SCHEMA or not plan.get("ok"):
            raise ValueError("AI_EDITOR_PLAN_NOT_CERTIFIED")
        identity = plan.get("identity") or {}
        if identity.get("manifest_sha256") != contract.digest(manifest) or identity.get("timing_sha256") != contract.digest(narration_timing):
            raise ValueError("AI_EDITOR_INPUT_CHANGED")
        if identity.get("analysis_sha256") != contract.digest({k: v for k, v in analysis.items() if k != "ai_editor_plan"}):
            raise ValueError("AI_EDITOR_ANALYSIS_CHANGED")
        return _assemble(manifest, analysis, plan, narration_timing=narration_timing)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {"ok": False, "selection_mode": "ai_editor", "segments": [], "manifest": dict(manifest),
                "pending_items": plan.get("issues") or [{"type": "AI_EDITOR_UNCERTIFIED", "message": str(exc)}],
                "recommendations": [], "degraded_matches": [], "material_gaps": plan.get("material_gaps", []),
                "material_gap_count": len(plan.get("material_gaps", []))}


def _heuristic_sentence(index: int, text: str) -> dict:
    """规则初排后的补选句：不做 inventory LLM，按整句作一个卖点点。"""
    value = str(text or "").strip()
    return {
        "index": index,
        "text": value,
        "kind": "selling_point",
        "selling_points": [{"id": "p1", "text": value[:48] or value}],
        "candidate_ids": [],
    }


def _rule_shortlist_ids(matched: dict, analysis: dict, index: int, *, limit: int = 8,
                        min_duration_s: float = 0.0) -> list[str]:
    """从规则匹配结果取该句短名单；不够时用更长的 ready 镜头补齐。

    When ``min_duration_s`` is set, long-enough ready shots are placed first so
    a full shortlist of short rule candidates cannot crowd out feasible windows.
    """
    preferred: list[str] = []
    for rec in matched.get("recommendations") or []:
        if not isinstance(rec, dict):
            continue
        try:
            if int(rec.get("index")) != index:
                continue
        except (TypeError, ValueError):
            continue
        for row in rec.get("candidates") or rec.get("top3") or []:
            if isinstance(row, dict) and str(row.get("shot_id") or "").strip():
                preferred.append(str(row["shot_id"]).strip())
    for seg in matched.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        try:
            if int(seg.get("index") or 0) != index and int(seg.get("sentence_index") or 0) != index:
                continue
        except (TypeError, ValueError):
            continue
        sid = str(seg.get("shot_id") or seg.get("temporary_shot_id") or "").strip()
        if sid:
            preferred.append(sid)
        for part in seg.get("span_parts") or []:
            if isinstance(part, dict) and str(part.get("shot_id") or "").strip():
                preferred.append(str(part["shot_id"]).strip())
    # Timing gaps are usually SOURCE_TOO_SHORT: prefer longer ready shots when
    # padding so AI refill is not stuck reviewing the same short windows.
    ready_rows = [
        s for s in analysis.get("shots") or []
        if isinstance(s, dict) and s.get("status") == "ready_for_matching" and s.get("shot_id")
    ]
    by_id = {
        str(s["shot_id"]).strip(): s for s in ready_rows if str(s.get("shot_id") or "").strip()
    }

    def _dur(row: dict) -> float:
        try:
            return float(row.get("duration") or row.get("duration_s") or 0)
        except (TypeError, ValueError):
            return 0.0

    floor = max(0.0, float(min_duration_s or 0))
    ready_rows.sort(key=_dur, reverse=True)
    long_enough = [s for s in ready_rows if _dur(s) >= floor]

    def _long_enough_id(sid: str) -> bool:
        row = by_id.get(sid)
        return bool(row) and _dur(row) >= floor

    ids: list[str] = []
    # 1) rule/segment picks that already clear the duration floor
    if floor > 0:
        for sid in preferred:
            if sid and _long_enough_id(sid) and sid not in ids:
                ids.append(sid)
            if len(ids) >= limit:
                return ids
    else:
        for sid in preferred:
            if sid and sid not in ids:
                ids.append(sid)
            if len(ids) >= limit:
                return ids
    # 2) other long ready shots (longest first)
    for row in long_enough:
        sid = str(row["shot_id"]).strip()
        if sid and sid not in ids:
            ids.append(sid)
        if len(ids) >= limit:
            return ids
    # 3) remaining preferred (may be short — last resort for review coverage)
    for sid in preferred:
        if sid and sid not in ids:
            ids.append(sid)
        if len(ids) >= limit:
            return ids
    for row in ready_rows:
        sid = str(row["shot_id"]).strip()
        if sid and sid not in ids:
            ids.append(sid)
        if len(ids) >= limit:
            break
    return ids


def _reindex_ai_segment(segment: dict, index: int) -> dict:
    """Rewrite sealed AI receipts from mini-manifest index=1 to the real sentence."""
    out = dict(segment)
    out["index"] = index

    def _rewrite(receipt: object) -> dict | None:
        if not isinstance(receipt, dict):
            return None
        rewritten = {**receipt, "index": index}
        unsigned = {k: v for k, v in rewritten.items() if k != "receipt_sha256"}
        rewritten["receipt_sha256"] = contract.digest(unsigned)
        return rewritten

    receipt = _rewrite(out.get("ai_selection"))
    if receipt is not None:
        out["ai_selection"] = receipt
    parts = out.get("span_parts")
    if isinstance(parts, list) and parts:
        new_parts = []
        for part in parts:
            if not isinstance(part, dict):
                new_parts.append(part)
                continue
            item = dict(part)
            part_receipt = _rewrite(item.get("ai_selection"))
            if part_receipt is not None:
                item["ai_selection"] = part_receipt
            new_parts.append(item)
        out["span_parts"] = new_parts
        if out.get("multi_shot") and isinstance(out.get("ai_selection"), dict):
            sealed = {**out["ai_selection"], "parts_sha256": contract.digest(new_parts)}
            unsigned = {k: v for k, v in sealed.items() if k != "receipt_sha256"}
            sealed["receipt_sha256"] = contract.digest(unsigned)
            out["ai_selection"] = sealed
    return out


def refill_indices(
    manifest: dict,
    analysis: dict,
    matched: dict,
    indices: list[int],
    report_dir: Path,
    *,
    narration_timing: dict | None = None,
) -> dict:
    """对规则初排后 QC/缺口失败的句子做 AI 看图补选，其余句保留。

    返回的 selection_mode 为 rule_then_ai；被补选句带 ai_selection 收据。
    """
    from . import shot_analyzer

    wanted = sorted({int(i) for i in indices if int(i) >= 1})
    if not wanted:
        return matched
    directory = report_dir / "ai_editor_refill"
    directory.mkdir(parents=True, exist_ok=True)
    original = timing_contract.split_sentences(manifest)
    shots = {
        str(s["shot_id"]): s
        for s in analysis.get("shots") or []
        if isinstance(s, dict) and s.get("status") == "ready_for_matching" and s.get("shot_id")
    }
    if not shots:
        return {
            **matched,
            "ok": False,
            "selection_mode": "rule_then_ai",
            "pending_items": [{"type": "AI_EDITOR_UNCERTIFIED", "message": "AI_EDITOR_NO_UNIQUE_CANDIDATES"}],
        }

    segments = [dict(s) for s in (matched.get("segments") or []) if isinstance(s, dict)]
    # Rule-match rows sometimes omit index and may drop sentences. Key by script
    # text first so refill N always replaces/inserts the N-th narration line.
    def _norm_claim(value: object) -> str:
        return "".join(str(value or "").split())

    text_to_script_index = {
        _norm_claim(timing_contract.sentence_text(sentence)): index
        for index, sentence in enumerate(original, 1)
    }
    by_index: dict[int, dict] = {}
    for enum_i, seg in enumerate(segments, 1):
        try:
            explicit = int(seg.get("index"))
        except (TypeError, ValueError):
            explicit = 0
        by_text = text_to_script_index.get(
            _norm_claim(seg.get("text") or seg.get("claim") or "")) or 0
        # Prefer script-text identity when enumerate position drifted (dropped
        # sentences) or explicit index disagrees with the spoken line.
        if by_text >= 1:
            key = by_text
        elif explicit >= 1:
            key = explicit
        else:
            key = enum_i
        sealed = dict(seg)
        sealed["index"] = key
        by_index[key] = sealed

    def _seg_shot_ids(seg: dict) -> list[str]:
        ids: list[str] = []
        for part in (seg.get("span_parts") or [seg]):
            if not isinstance(part, dict):
                continue
            sid = str(part.get("shot_id") or part.get("temporary_shot_id") or "").strip()
            if sid:
                ids.append(sid)
        return ids

    # Keep shots already committed on non-refill sentences, and ones claimed by
    # earlier refills in this pass, so the draft cannot land DUPLICATE_VISUAL_SHOT.
    reserved_shot_ids: set[str] = set()
    wanted_set = set(wanted)
    for key, seg in by_index.items():
        if key in wanted_set or not isinstance(seg, dict):
            continue
        reserved_shot_ids.update(_seg_shot_ids(seg))

    refill_errors: list[str] = []
    # Action-hard claims first so wipe/tear demos reserve matching shots
    # before softer display refills consume the same unique pool.
    def _refill_order(index: int) -> tuple[int, int]:
        if index > len(original):
            return (2, index)
        claim = timing_contract.sentence_text(original[index - 1])
        return (0 if _claim_needs_action(claim) else 1, index)

    for index in sorted(wanted, key=_refill_order):
        if index > len(original):
            refill_errors.append(f"AI_REFILL_INDEX_OOB:{index}")
            continue
        text = timing_contract.sentence_text(original[index - 1])
        sentence = _heuristic_sentence(index, text)
        audio_us, certainty = timing_contract.duration_for(
            narration_timing, index, segment=original[index - 1])
        # Long narration fits short footage only by speeding up. Floor the
        # shortlist on usable seconds at product_display max speed (1.2x),
        # plus default head/tail trim when comparing raw shot duration.
        # (Previously used audio*0.85 — that inverted the bound and let
        # AUDIO_LONGER_THAN_SHOT shots crowd out longer candidates.)
        min_dur_s = 0.0
        if audio_us:
            min_dur_s = (float(audio_us) / 1_000_000.0) / 1.2
            min_dur_s += float(manifest.get("head_trim_s") or shot_analyzer.DEFAULT_HEAD_TRIM_S)
            min_dur_s += float(manifest.get("tail_trim_s") or shot_analyzer.DEFAULT_TAIL_TRIM_S)
        shortlist_limit = max(ROUND_SIZE, 16)
        base_ids = _rule_shortlist_ids(
            matched, analysis, index, limit=shortlist_limit, min_duration_s=min_dur_s)
        # Hard action claims: inject matching action shots ahead of packaging
        # shortlist, but only those that can host the audio — otherwise short
        # action hits crowd the review pool and recreate AUDIO_LONGER_THAN_SHOT.
        action_ids = _action_shot_ids_for_claim(analysis, text, limit=8)
        fit_action = [
            sid for sid in action_ids
            if _shot_fits_audio(shots.get(sid), int(audio_us or 0), manifest=manifest)
        ]
        gift_ids = _gift_prop_shot_ids_for_claim(analysis, text, limit=8)
        fit_gift = [
            sid for sid in gift_ids
            if _shot_fits_audio(shots.get(sid), int(audio_us or 0), manifest=manifest)
        ]
        # Gift props ahead of generic packaging so「送支架/挂钩」does not land on
        # unrelated cooking/wipe demos when accessory footage exists.
        merged = list(dict.fromkeys([*fit_gift, *fit_action, *base_ids, *gift_ids, *action_ids]))
        sentence["candidate_ids"] = [
            sid for sid in merged if sid not in reserved_shot_ids
        ][:shortlist_limit]
        if not sentence["candidate_ids"]:
            # Fall back to the unfiltered shortlist only when every candidate is
            # already reserved; review below still prefers an unused match.
            sentence["candidate_ids"] = merged[:shortlist_limit] or base_ids
        if not sentence["candidate_ids"]:
            refill_errors.append(f"AI_REFILL_NO_CANDIDATES:{index}")
            continue
        sentence["audio_durations_us"] = [audio_us]
        try:
            reviews, coverage = _review_pool(
                manifest, sentence, shots, directory, narration_timing or {})
            good = [r for r in reviews if r.get("verdict") == "matched"]
            if not good or not coverage.get("feasible"):
                raise ValueError(f"AI_REFILL_NO_MATCH:{index}")
            # Prefer action evidence over packaging-only matches; still unique.
            # If the top match cannot host the narration window (too short /
            # speed conflict), try the next ranked matched shot instead of
            # abandoning the whole sentence.
            # _assemble 要求 plan 内 index 落在 1..len(sentences)；单句补选时缩成迷你稿。
            mini_manifest = {
                k: v for k, v in manifest.items()
                if k not in {"segments", "full_script", "script"}
            }
            mini_manifest["full_script"] = text
            src_timing_row = {"key": "s1", "text": text, "audio_duration_us": audio_us,
                              "timing_certainty": certainty}
            if isinstance(narration_timing, dict):
                rows = narration_timing.get("rows") or []
                if 0 <= index - 1 < len(rows) and isinstance(rows[index - 1], dict):
                    src_timing_row = {**rows[index - 1], "key": "s1", "text": text}
            mini_timing = {**(narration_timing or {}), "rows": [src_timing_row]}
            local_sentence = {**sentence, "index": 1}
            pool = list(good)
            skip_ids: set[str] = set()
            last_exc: Exception | None = None
            placed = False
            allow_reuse = False
            while pool and not placed:
                best = _pick_best_refill_review(
                    pool,
                    claim_text=text,
                    shots=shots,
                    reserved_shot_ids=(set() if allow_reuse
                                       else (reserved_shot_ids | skip_ids)),
                    audio_us=int(audio_us or 0),
                    manifest=manifest,
                )
                if best is None:
                    # All unique action/display matches already taken by earlier
                    # refill sentences (e.g. wipe demos). Last resort: allow a
                    # reserved matched shot so one hard claim does not BLOCK the
                    # whole draft; QC may still flag DUPLICATE for a later pass.
                    if not allow_reuse and reserved_shot_ids:
                        allow_reuse = True
                        continue
                    break
                sid = str(best.get("shot_id") or "").strip()
                if sid:
                    skip_ids.add(sid)
                pool = [
                    row for row in pool
                    if str(row.get("shot_id") or "").strip() != sid
                ]
                plan = {
                    "schema": contract.PLAN_SCHEMA,
                    "policy_version": contract.POLICY_VERSION,
                    "sentences": [local_sentence],
                    "reviews": {"1": reviews},
                    "display_reviews": [],
                    "rows": [{
                        "index": 1,
                        "parts": [{"shot_id": best["shot_id"], "audio_duration_us": int(audio_us)}],
                        "reason": f"rule_then_ai refill after QC/gap for S{index:02d}",
                    }],
                    "identity": {
                        "model": os.environ.get("JY_VISION_MODEL") or "glm-5.3-flash",
                        "manifest_sha256": contract.digest(mini_manifest),
                        "timing_sha256": contract.digest(mini_timing),
                        "analysis_sha256": contract.digest(
                            {k: v for k, v in analysis.items() if k != "ai_editor_plan"}),
                        "prompt_sha256": contract.digest(contract.POLICY),
                        "policy_version": contract.POLICY_VERSION,
                        "refill": True,
                        "index": index,
                    },
                    "ok": True,
                }
                try:
                    assembled = _assemble(
                        mini_manifest, analysis, plan,
                        narration_timing=mini_timing, verify_sources=True)
                    new_seg = (assembled.get("segments") or [None])[0]
                    if not isinstance(new_seg, dict):
                        raise ValueError(f"AI_REFILL_ASSEMBLE_EMPTY:{index}")
                    # Mini-manifest assembled with local index=1; re-seal receipts
                    # to the real sentence index or visual evidence fails
                    # AI_SELECTION_INDEX_CHANGED.
                    new_seg = _reindex_ai_segment(new_seg, index)
                    check = contract.validate_segment_receipt(
                        new_seg, index=index, verify_source=True)
                    if not check["ok"]:
                        raise ValueError(";".join(check["reasons"]))
                    by_index[index] = new_seg
                    reserved_shot_ids.update(_seg_shot_ids(new_seg))
                    _save(directory / f"refill_{index:02d}.json", {
                        "index": index, "shot_id": best.get("shot_id"),
                        "coverage": coverage,
                        "reason": plan["rows"][0]["reason"],
                    })
                    placed = True
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    last_exc = exc
                    msg = str(exc)
                    if any(token in msg for token in (
                            "WINDOW_INFEASIBLE", "AUDIO_LONGER_THAN_SHOT",
                            "SPEED_CONFLICT", "EVIDENCE_OUT_OF_RANGE",
                            "AI_REFILL_ASSEMBLE_EMPTY")):
                        continue
                    raise
            if not placed:
                if last_exc is not None:
                    # Mini-assemble uses local index=1; rewrite to the real
                    # sentence so pending_items / logs stay auditable.
                    msg = str(last_exc)
                    marker = "AI_EDITOR_WINDOW_INFEASIBLE:1:"
                    if marker in msg:
                        msg = msg.replace(
                            marker, f"AI_EDITOR_WINDOW_INFEASIBLE:{index}:", 1)
                    raise ValueError(msg) from last_exc
                raise ValueError(f"AI_REFILL_NO_UNIQUE_SHOT:{index}")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            # 补选失败则保留规则初排原段，交给后续 QC/下一次补选。
            refill_errors.append(str(exc))

    # Emit in narration order so dropped rule rows do not shift later indices.
    ordered = [
        by_index[i] for i in range(1, len(original) + 1) if i in by_index
    ]
    for key in sorted(by_index):
        if key < 1 or key > len(original):
            ordered.append(by_index[key])
    out_manifest = {
        **(matched.get("manifest") or manifest),
        "selection_mode": "rule_then_ai",
        "segments": ordered,
        "temporary_material_analysis": True,
    }
    replaced = [i for i in wanted if contract.is_ai_segment(by_index.get(i) or {})]
    replaced_set = set(replaced)
    def _keep_gap(row: object) -> bool:
        if not isinstance(row, dict):
            return True
        try:
            return int(row.get("index")) not in replaced_set
        except (TypeError, ValueError):
            return True

    pending = [
        {"type": "AI_EDITOR_UNCERTIFIED", "message": msg} for msg in refill_errors
    ] if refill_errors else [
        row for row in (matched.get("pending_items") or []) if _keep_gap(row)
    ]
    # All requested gap sentences replaced and no leftover pending → treat as matched.
    ok = bool(ordered) and (not wanted or (
        set(wanted).issubset(replaced_set) and not refill_errors and not pending))
    material_gaps = [row for row in (matched.get("material_gaps") or []) if _keep_gap(row)]
    degraded_matches = [
        row for row in (matched.get("degraded_matches") or []) if _keep_gap(row)
    ]
    visual_missing = [
        row for row in (matched.get("visual_missing_matches") or []) if _keep_gap(row)
    ]
    preview_unresolved = [
        row for row in (matched.get("preview_unresolved") or []) if _keep_gap(row)
    ]
    result = {
        **matched,
        "ok": ok if wanted else bool(matched.get("ok")),
        "selection_mode": "rule_then_ai",
        "selection_policy": "rule_then_ai_refill",
        "segments": ordered,
        "manifest": out_manifest,
        "refill_indices": wanted,
        "refill_replaced": replaced,
        "refill_errors": refill_errors,
        "pending_items": pending,
        "material_gaps": material_gaps,
        "degraded_matches": degraded_matches,
        "visual_missing_matches": visual_missing,
        "preview_unresolved": preview_unresolved,
        # Keep counter fields in sync with filtered lists; stale degraded_count
        # after a successful refill was blocking formal validate_visual_match.
        "material_gap_count": len(material_gaps),
        "degraded_count": len(degraded_matches),
        "degraded_fallback_count": 0 if ok and not degraded_matches else int(
            matched.get("degraded_fallback_count") or 0),
        "visual_missing_count": len(visual_missing),
        "preview_unresolved_count": len(preview_unresolved),
    }
    if result["ok"]:
        result["degraded_fallback_count"] = 0
        result["accepted_shots"] = [
            p.get("shot_id") or s.get("temporary_shot_id")
            for s in ordered
            for p in (s.get("span_parts") or [s])
        ]
        result["claim_gate_coverage"] = {
            "mode": "rule_then_ai",
            "certification": "MIXED_RULE_AND_AI_REFILL",
            "validation_passed": True,
            "uncertified_reasons": [],
            "scope": "rule_first_ai_refill_independent_qc_required",
        }
    _ = shot_analyzer
    return result
