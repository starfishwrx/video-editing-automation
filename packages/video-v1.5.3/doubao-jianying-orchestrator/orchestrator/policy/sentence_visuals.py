"""Resolve an operator's shot direction only for the approved order and script revision."""
from __future__ import annotations

import hashlib
import re


def script_digest(script):
    return hashlib.sha256(script.strip().encode("utf-8")).hexdigest()


def approved(manifest, index, spoken):
    entries = manifest.get("sentence_visual_approvals", [])
    if not entries:
        return None
    if not isinstance(entries, list):
        raise ValueError("SENTENCE_VISUAL_APPROVALS_INVALID")
    task = manifest.get("task_id") or manifest.get("record_id")
    product = manifest.get("product")
    script = manifest.get("full_script") or manifest.get("script")
    if not all(isinstance(v, str) and v.strip() for v in (task, product, script, spoken)):
        return None
    digest = script_digest(script)
    matches = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("SENTENCE_VISUAL_APPROVAL_INVALID")
        if (entry.get("task_id") != task or entry.get("product") != product
                or entry.get("script_sha256") != digest or type(entry.get("index")) is not int
                or entry["index"] != index or entry.get("text") != spoken):
            continue
        if (entry.get("mode") not in {"display", "absorption"}
                or not all(isinstance(entry.get(k), str) and entry[k].strip() for k in ("show", "source"))):
            raise ValueError("SENTENCE_VISUAL_APPROVAL_INVALID")
        matches.append({key: entry[key] for key in ("task_id", "product", "script_sha256", "index",
                                                     "text", "mode", "show", "source")})
    if len(matches) > 1:
        raise ValueError("SENTENCE_VISUAL_APPROVAL_AMBIGUOUS")
    return matches[0] if matches else None


def project_single(manifest, index, spoken, single):
    """Bind a one-row helper manifest to the already resolved original order."""
    entry = approved(manifest, index, spoken)
    single["sentence_visual_approvals"] = ([{**entry, "index": 1, "script_sha256": script_digest(spoken)}]
                                          if entry else [])
    return single["sentence_visual_approvals"][0] if entry else None


def goal(approval):
    return ("本单已确认这句口播的画面承接方式。保留原文和原声，独立检查本句实拍是否展示"
            + approval["show"] + "。仅报告看得见的动作或标示，该展示不构成原数字的实测证明。")


def presentation_visible(approval, capture, cuts, semantic_observation):
    if (not isinstance(capture, dict) or capture.get("approval") != approval or capture.get("state") != "visible"
            or capture.get("main_product") is not True or not isinstance(capture.get("observation"), str)
            or not capture["observation"].strip()):
        return False
    observed = str(semantic_observation or "") + "。" + capture["observation"]
    for absent in re.findall(r"(?:没有看到|没有出现|未见到|未(?:能)?看见|未出现|看不到|没拍到|未展示)([^。；，,]{1,18})", observed):
        if any(word in absent and word in approval["show"] for word in
               ("包装", "商品", "巾体", "护翼", "腰围", "拉伸", "展开", "倒液", "吸水", "吸收", "品牌", "标识", "正面", "提起")):
            return False
    frames = capture.get("cited_frames")
    return bool(isinstance(frames, list) and frames and all(
        isinstance(f, list) and len(f) == 2 and all(type(v) is int for v in f) and f[0] in cuts
        and cuts[f[0]][0] <= f[1] < cuts[f[0]][1] for f in frames))


def apply_to_sentence(row, approval):
    # The language model may suggest shots but cannot create an operator approval.
    row.pop("approved_visual", None)
    if approval:
        row.update(approved_visual=approval, kind="selling_point", scene_ids=[],
                   selling_points=[{"id": "approved_visual", "text": approval["show"]}],
                   experiment_role="absorption" if approval["mode"] == "absorption" else "other")
        row.pop("hook_neighbor", None)
