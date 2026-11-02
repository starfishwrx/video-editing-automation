"""Replay paper shot selection from existing observations and fixed narration.

This module does not synthesize speech, recognize a material library, write a
Jianying project or deliver anything. Descriptions retrieve candidates; fresh
window review supplies the action boundaries used by the retiming solver.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
from typing import Callable

from .. import ai_editor_contract as contract
from . import paper_retiming

PROTOCOL = "paper-shot-selection-replay-v1"
KINDS = {"setup", "process", "key_action", "result", "display"}


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def validate_observation(row: dict, shot: dict, request: dict) -> dict:
    """Require explicit speed permission and an unbroken reviewed source span."""
    if not isinstance(row, dict) or row.get("verdict") not in {"matched", "mismatch", "uncertain"}:
        raise ValueError("PAPER_REVIEW_VERDICT_INVALID")
    if row["verdict"] != "matched":
        return row
    confidence = row.get("confidence")
    if (row.get("same_product") is not True or row.get("supports_requirement") is not True
            or type(confidence) not in (int, float) or not .6 <= confidence <= 1 or not math.isfinite(confidence)):
        raise ValueError("PAPER_REVIEW_SUPPORT_UNCONFIRMED")
    if not all(isinstance(row.get(k), str) and row[k].strip() for k in ("observation", "reason")):
        raise ValueError("PAPER_REVIEW_OBSERVATION_REQUIRED")
    window = row.get("window_us")
    lo, hi = round(float(shot["source_start"]) * 1e6), round(float(shot["source_end"]) * 1e6)
    if (not isinstance(window, dict) or any(type(window.get(k)) is not int for k in ("start", "end"))
            or not lo <= window["start"] < window["end"] <= hi):
        raise ValueError("PAPER_REVIEW_WINDOW_INVALID")
    evidence = row.get("evidence_intervals_us")
    if (not isinstance(evidence, list) or not evidence or any(
            not isinstance(ev, dict) or any(type(ev.get(k)) is not int for k in ("start", "end"))
            or not window["start"] <= ev["start"] < ev["end"] <= window["end"] for ev in evidence)):
        raise ValueError("PAPER_REVIEW_EVIDENCE_OUTSIDE_WINDOW")
    phases = row.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ValueError("PAPER_REVIEW_PHASES_REQUIRED")
    cursor = window["start"]
    for phase in phases:
        if (not isinstance(phase, dict) or any(type(phase.get(k)) is not int for k in ("start", "end"))
                or phase["start"] != cursor or not phase["start"] < phase["end"] <= window["end"]
                or phase.get("kind") not in KINDS or type(phase.get("can_speed")) is not bool
                or not isinstance(phase.get("reason"), str) or not phase["reason"].strip()):
            raise ValueError("PAPER_REVIEW_PHASES_INVALID")
        if phase["kind"] in {"key_action", "result"} and phase["can_speed"]:
            raise ValueError("PAPER_REVIEW_PROTECTED_PHASE_ACCELERATED")
        cursor = phase["end"]
    if cursor != window["end"]:
        raise ValueError("PAPER_REVIEW_PHASES_INCOMPLETE")
    if request.get("require_action") and not any(p["kind"] == "key_action" for p in phases):
        raise ValueError("PAPER_REVIEW_KEY_ACTION_MISSING")
    if request.get("require_result") and not any(p["kind"] == "result" for p in phases):
        raise ValueError("PAPER_REVIEW_RESULT_MISSING")
    if type(row.get("time_sensitive")) is not bool:
        raise ValueError("PAPER_REVIEW_TIME_POLICY_REQUIRED")
    return row


def review_candidate(manifest: dict, request: dict, shot: dict, directory: Path) -> dict:
    """Use the existing GLM/Qwen adapter; it owns retries and account slots."""
    from . import ai_editor
    media = ai_editor._review_media(shot, directory, dense=True, fit=True)
    context = {
        "product": manifest.get("product"), "category": manifest.get("category"),
        "sentence": request["text"], "editing_feedback": request["requirement"],
        "target_duration_us": request["segment_us"], "require_action": bool(request.get("require_action")),
        "require_result": bool(request.get("require_result")),
        "source_window_us": {"start": round(media["start"] * 1e6), "end": round(media["end"] * 1e6)},
    }
    prompt = """你是纸品广告的选镜编辑。只根据这次提供的有时间戳图片判断，不相信素材文件名或历史通过结论。
选取最短但完整的一次有效演示，满足该句及修改反馈。保持原配音时长，不修改文案。
同一镜头内部必须连续，不跳过中间动作。可去掉动作前后无关等待及其他重复演示。
关键动作和看清结果必需的部分保持1倍速；只有明确可压缩的准备、行进、非关键重复过程可加速，优先不超过2倍，最多3倍。
时间性声称的证明过程不可加速。普通包装、整箱和材质展示按本句用途判断，不要求编造实验。
对于“瓷实”要看单包包装厚实饱满；裸纸扇形展开不能替代包装。对于“不连抽”必须实际看见单次抽取，静态抚摸不是抽取。
亲肤体验不要求虚构物理性能，但要包含一次看得清的接触动作。揉搓应保留一次必要动作及完整纸张结果，不要把多次重复揉搓全标为必需。
揉搓等非计时演示应区分关键接触、可压缩的中间重复过程、展开与结果；只有图片足以确认时，才把不影响证明的中间过程标成process且can_speed=true。
不能为了塞入配音把必要动作改名为process；开始施力、确认完成与完整结果仍保留原速。若确实全程必需，明确保留，让时长求解返回冲突。
相邻16张图片只是采样。若无法确认动作开始、完成或结果，返回uncertain，不能凭文字补全未拍到的过程。
输出JSON：{"verdict":"matched|mismatch|uncertain","same_product":true,"supports_requirement":true,
"confidence":0.8,"observation":"可见事实","reason":"为何满足本句和反馈","time_sensitive":false,
"window_us":{"start":1000000,"end":2500000},"evidence_intervals_us":[{"start":1200000,"end":2400000}],
"phases":[{"start":1000000,"end":1500000,"kind":"setup|process|key_action|result|display","can_speed":false,"reason":"依据"}]}。
所有时间为源素材绝对微秒；phases必须按顺序无缝覆盖window_us，key_action/result的can_speed必须false。
必要时包含同一动作前后贴切的展示以填满配音，不能让window短于配音后靠定格补足。不要为凑时长缩掉证据。
""" + json.dumps(context, ensure_ascii=False)
    key = contract.digest([PROTOCOL, context, media["before"], str(shot["shot_id"])])[:24]
    row = ai_editor._call(prompt, directory / "reviews" / (key + ".json"), Path(media["sheet"]))
    if media["before"] != contract.source_identity(shot["video"]):
        raise ValueError("PAPER_SOURCE_CHANGED_DURING_REVIEW")
    row = validate_observation(row, shot, request)
    return dict(row, source_identity=media["before"], sheet=str(media["sheet"]),
                reviewed_window_us=context["source_window_us"])


def plan_candidate(observation: dict, shot: dict, request: dict, *, fps: float = 30) -> dict:
    row = validate_observation(observation, shot, request)
    if row["verdict"] != "matched":
        return {"ok": False, "reason": "PIXEL_MATCH_NOT_CONFIRMED"}
    window = row["window_us"]
    protected = [{"start": p["start"], "end": p["end"]} for p in row["phases"]
                 if not p["can_speed"] or row["time_sensitive"]]
    result = paper_retiming.plan_retiming(window["start"], window["end"], protected,
        request["segment_us"], fps=fps, preferred_cap=2.0, hard_cap=3.0)
    if result["ok"]:
        result["parts"] = [dict(p, source=str(shot["video"])) for p in result["parts"]]
    return result


def reselect(manifest: dict, analysis: dict, requests: list[dict], directory: Path, *,
             reviewer: Callable | None = None, workers: int = 2) -> dict:
    """Review at most three explicit retrieved candidates per fixed sentence.

    Candidate IDs retrieve already analyzed shots. No library recognition or
    speculative fallback is reachable from this entrypoint.
    """
    from .. import rulepack
    if rulepack.detect_category(manifest)[0] != rulepack.CATEGORY_ZHIPIN:
        raise ValueError("PAPER_SELECTION_CATEGORY_REQUIRED")
    reviewer = reviewer or review_candidate
    shots = {}
    for shot in analysis.get("shots", []):
        if not isinstance(shot, dict) or shot.get("status") != "ready_for_matching":
            continue
        sid = shot.get("shot_id")
        if not isinstance(sid, str) or not sid.strip() or sid in shots:
            raise ValueError("PAPER_ANALYSIS_SHOT_ID_INVALID_OR_DUPLICATE")
        shots[sid] = shot
    if not isinstance(requests, list) or not requests or any(not isinstance(r, dict) for r in requests):
        raise ValueError("PAPER_REPLAY_REQUEST_INVALID")
    seen, jobs, rows = set(), [], []
    for request in requests:
        index = request.get("index")
        ids = request.get("candidate_ids")
        if (type(index) is not int or index < 1 or index in seen
                or type(request.get("segment_us")) is not int or request["segment_us"] <= 0
                or not all(isinstance(request.get(k), str) and request[k].strip() for k in ("text", "requirement"))
                or not isinstance(ids, list) or not 1 <= len(ids) <= 3
                or any(not isinstance(sid, str) or not sid.strip() for sid in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("PAPER_REPLAY_REQUEST_INVALID")
        seen.add(index)
        if request.get("candidate_windows") is not None and not isinstance(request["candidate_windows"], dict):
            raise ValueError("PAPER_REVIEW_HINT_INVALID")
        row = {"index": index, "text": request["text"], "requirement": request["requirement"],
               "segment_us": request["segment_us"], "candidates": []}
        rows.append(row)
        for sid in ids:
            if sid not in shots:
                raise ValueError("PAPER_CANDIDATE_NOT_READY:" + str(sid))
            # Optional bounded source subwindow is only a review region, never
            # an observation or a permission to silently trim old evidence.
            shot = dict(shots[sid])
            hint = (request.get("candidate_windows") or {}).get(sid)
            if hint is not None:
                if (not isinstance(hint, dict) or any(type(hint.get(k)) is not int for k in ("start", "end"))
                        or not round(float(shot["source_start"]) * 1e6) <= hint["start"] < hint["end"] <= round(float(shot["source_end"]) * 1e6)):
                    raise ValueError("PAPER_REVIEW_HINT_INVALID")
                shot.update(source_start=hint["start"] / 1e6, source_end=hint["end"] / 1e6)
            jobs.append((row, request, shot))

    def run(job):
        row, request, shot = job
        result = {"shot_id": shot["shot_id"], "source": str(shot["video"])}
        try:
            before = contract.source_identity(shot["video"])
            observation = reviewer(manifest, request, shot, directory)
            observation = validate_observation(observation, shot, request)
            if before != contract.source_identity(shot["video"]):
                raise ValueError("PAPER_SOURCE_CHANGED_DURING_REVIEW")
            if observation.get("source_identity") not in (None, before):
                raise ValueError("PAPER_REVIEW_STALE_SOURCE")
            observation = dict(observation, source_identity=before)
            plan = plan_candidate(observation, shot, request, fps=float(manifest.get("fps", 30)))
            result.update(observation=observation, plan=plan)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            result.update(error=str(exc), plan={"ok": False, "reason": "REVIEW_UNCERTIFIED"})
        return row, result

    with ThreadPoolExecutor(max_workers=max(1, min(2, int(workers)))) as pool:
        for row, result in pool.map(run, jobs):
            row["candidates"].append(result)
            _write(directory / ("sentence-%02d.json" % row["index"]), row)
    overrides, unresolved = [], []
    for row in rows:
        feasible = [r for r in row["candidates"] if r["plan"].get("ok")]
        if not feasible:
            reason = ("DURATION_FIT_CONFLICT" if any(r.get("observation", {}).get("verdict") == "matched" for r in row["candidates"])
                      else "EVIDENCE_NOT_CONFIRMED")
            row.update(status="UNRESOLVED", reason=reason)
            unresolved.append({"index": row["index"], "reason": reason})
            continue
        chosen = min(feasible, key=lambda r: (
            r["plan"].get("tier") == "EXCEPTION", r["plan"]["maximum_speed"],
            -float(r["observation"]["confidence"]), len(r["plan"]["parts"])))
        row.update(status="SELECTED", selected_shot_id=chosen["shot_id"],
                   maximum_speed=chosen["plan"]["maximum_speed"], tier=chosen["plan"]["tier"])
        overrides.append({"index": row["index"], "reason": chosen["observation"]["reason"],
                          "parts": chosen["plan"]["parts"], "shot_id": chosen["shot_id"],
                          "observation": chosen["observation"], "retiming_plan": chosen["plan"]})
    report = {"schema": PROTOCOL, "status": "SELECTION_PARTIAL" if unresolved else "SELECTION_READY",
              "preview_only": True, "rows": rows, "overrides": overrides, "unresolved": unresolved,
              "preserved": ["narration", "sentence_duration", "subtitles", "bgm"],
              "preferred_speed_cap": 2.0, "hard_speed_cap": 3.0}
    _write(directory / "selection.json", report)
    return report
