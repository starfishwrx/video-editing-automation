#!/usr/bin/env python3
"""Post-build visual/structure gate for JianYing drafts.

This is intentionally independent from shot matching: it opens the generated
draft, extracts fresh frames from the placed source ranges, checks the draft
timeline/media graph, and asks the configured vision model to judge the
selected frames against the script claims.  A successful build alone is never
treated as acceptance.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

try:
    from .json_extract import EmptyResponseError, is_blank, iter_json_values
    from .video_decode import run_ffmpeg_decode
    from .visual_qc_contract import (MATCH_LEVELS, REPORT_VERSION, RESPONSE_VERSION,
                                     draft_metadata_fingerprint, normalize_match_level,
                                     response_errors)
except ImportError:
    # 这个文件会被 python draft_visual_qc.py 直接启动，没有包上下文。
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from orchestrator.json_extract import EmptyResponseError, is_blank, iter_json_values
    from orchestrator.video_decode import run_ffmpeg_decode
    from orchestrator.visual_qc_contract import (MATCH_LEVELS, REPORT_VERSION, RESPONSE_VERSION,
                                                draft_metadata_fingerprint, normalize_match_level,
                                                response_errors)


def _claim_gate_summary(claim: dict[str, Any]) -> str:
    gate = claim.get("claim_gate") or {}
    required = gate.get("required") if isinstance(gate, dict) else {}
    if not isinstance(required, dict):
        required = {}
    fields = {
        "gate_status": gate.get("status", "") if isinstance(gate, dict) else "",
        "required_role": required.get("required_role", ""),
        "required_action": required.get("required_action", ""),
        "required_result": required.get("required_result", ""),
        "evidence_status": required.get("evidence_status", ""),
    }
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


def _normalize_match_level(row: dict[str, Any]) -> str:
    return normalize_match_level(row)


def _hard_issue(issue: str) -> bool:
    return str(issue).startswith((
        "VIDEO_TRACK_MISSING",
        "SEGMENT_",
        "TIMELINE_GAP",
        "SINGLE_SOURCE_FOR_MULTI_SEGMENT_DRAFT",
        "PLACEHOLDER_COUNT_MISMATCH",
        "MATCH_REPORT_MISSING_OR_EMPTY",
        "POST_DRAFT_VISUAL_MISMATCH",
        "AI_QC_",
        "AI_SELECTION_",
    ))


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    for candidate in (path / "draft_info.json", path / "draft_content.json"):
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8-sig"))
    raise FileNotFoundError("draft_info.json/draft_content.json missing")


def _ffmpeg() -> str | None:
    return os.environ.get("JY_FFMPEG") or shutil.which("ffmpeg")


def _is_file(path: pathlib.Path) -> bool:
    """Treat an unavailable network share as a miss and try draft-local media."""
    try:
        return path.is_file()
    except OSError:
        # Windows raises WinError 1326/53 here when a UNC session is stale or
        # unavailable.  Drafts produced by the pipeline carry a local media
        # copy, so an SMB authentication failure must not abort QC before the
        # fallback is attempted.
        return False


def _material_map(draft: pathlib.Path, info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in (info.get("materials") or {}).get("videos") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or item.get("material_id") or "").strip()
        if not key:
            continue
        raw = str(item.get("path") or item.get("media_path") or "").strip()
        path = pathlib.Path(raw) if raw else pathlib.Path()
        if not _is_file(path):
            path = draft / "media" / pathlib.Path(raw).name
        result[key] = {"item": item, "path": path}
    return result


def _video_track(info: dict[str, Any]) -> dict[str, Any] | None:
    tracks = [t for t in info.get("tracks") or [] if isinstance(t, dict)]
    preferred = [t for t in tracks if str(t.get("name") or "").casefold() in {
        "video_broll", "video", "main video", "主视频", "视频"
    }]
    candidates = preferred or [t for t in tracks if t.get("segments")]
    return max(candidates, key=lambda t: len(t.get("segments") or []), default=None)


def _extract(ffmpeg: str, source: pathlib.Path, seconds: float, output: pathlib.Path) -> tuple[bool, str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    proc = run_ffmpeg_decode(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, seconds):.3f}",
         "-i", str(source), "-frames:v", "1", "-q:v", "2", str(output)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    ok = proc.returncode == 0 and output.is_file() and output.stat().st_size > 0
    return ok, (proc.stderr or "")[-500:]


def _black_hint(ffmpeg: str, source: pathlib.Path, seconds: float) -> bool:
    """Return true only when the sampled frame is strongly indicated black."""
    proc = run_ffmpeg_decode(
        [ffmpeg, "-hide_banner", "-loglevel", "info", "-ss", f"{max(0.0, seconds):.3f}",
         "-i", str(source), "-frames:v", "1", "-vf", "blackdetect=d=0.05:pix_th=0.08",
         "-an", "-f", "null", "-"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    return bool(re.search(r"black_start:\s*\d", (proc.stderr or "")))


def _timeline_items(info: dict[str, Any], draft: pathlib.Path) -> tuple[list[dict[str, Any]], list[str]]:
    track = _video_track(info)
    if not track:
        return [], ["VIDEO_TRACK_MISSING"]
    materials = _material_map(draft, info)
    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    for idx, segment in enumerate(track.get("segments") or [], 1):
        if not isinstance(segment, dict):
            issues.append(f"SEGMENT_{idx}_MALFORMED")
            continue
        material_id = str(segment.get("material_id") or "").strip()
        material = materials.get(material_id)
        if not material:
            issues.append(f"SEGMENT_{idx}_MATERIAL_MISSING:{material_id}")
            continue
        source = material["path"]
        material_name = str((material.get("item") or {}).get("material_name") or source.name)
        source_range = segment.get("source_timerange") or {}
        target_range = segment.get("target_timerange") or {}
        source_start = _num(source_range.get("start")) / 1_000_000
        source_duration = _num(source_range.get("duration")) / 1_000_000
        target_start = _num(target_range.get("start")) / 1_000_000
        target_duration = _num(target_range.get("duration")) / 1_000_000
        if not _is_file(source):
            issues.append(f"SEGMENT_{idx}_MEDIA_MISSING:{source}")
        if source_duration <= 0 or target_duration <= 0:
            issues.append(f"SEGMENT_{idx}_INVALID_RANGE")
        rows.append({
            "index": idx,
            "material_id": material_id,
            "source": str(source),
            "source_start": source_start,
            "source_duration": source_duration,
            "target_start": target_start,
            "target_duration": target_duration,
            "intentional_gap": material_name.startswith("visual_missing_placeholder")
            or source.name.startswith("visual_missing_placeholder"),
            "segment": segment,
        })
    rows.sort(key=lambda r: (r["target_start"], r["index"]))
    for left, right in zip(rows, rows[1:]):
        gap = right["target_start"] - (left["target_start"] + left["target_duration"])
        if gap > 0.05:
            issues.append(f"TIMELINE_GAP:{gap:.3f}s:after_segment_{left['index']}")
    return rows, issues


def _expected_claims(report_dir: pathlib.Path) -> list[dict[str, Any]]:
    path = report_dir / "shot_match_report.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    result = []
    for idx, row in enumerate(data.get("segments") or [], 1):
        if not isinstance(row, dict):
            continue
        parts = row.get("span_parts") if row.get("multi_shot") else None
        result.append({
            # 一句口播在时间线上占几个物理片段（场景分段变速、多镜段会拆成多段）。
            "slots": len(parts) if isinstance(parts, list) and len(parts) >= 2 else 1,
            "scene_rule": row.get("scene_rule") if isinstance(row.get("scene_rule"), dict) else {},
            "index": idx,
            "claim": str(row.get("claim_text") or row.get("text") or ""),
            "shot_id": str(row.get("temporary_shot_id") or ""),
            "selection_mode": str(row.get("selection_mode") or ""),
            "degraded": bool(row.get("degraded_no_match")
                             or row.get("cta_product_display_fallback")
                             or row.get("preview_unresolved")),
            "visual_missing": bool(row.get("visual_missing")),
            "claim_gate": row.get("claim_gate") or {},
            "sentence_kind": row.get("sentence_kind"),
            "ai_segment": row if row.get("selection_mode") == "ai_editor" else None,
            "product": (data.get("manifest") or {}).get("product", ""),
        })
    return result


def _prompt(claims: list[dict[str, Any]], frame_count: int) -> str:
    if any(c.get("selection_mode") == "ai_editor" for c in claims):
        from orchestrator import ai_editor_qc
        return ai_editor_qc.prompt(claims, frame_count)
    mapping = "\n".join(
        f"- S{local_index:02d}: claim={c['claim']}; expected_shot={c['shot_id']}; "
        f"selection={c['selection_mode']}; degraded={c['degraded']}; parts={c.get('slots', 1)}; "
        f"gate={_claim_gate_summary(c)}" for local_index, c in enumerate(claims, 1)
    )
    return f"""你是成片生成后的独立视觉验收员。图片是刚从已生成的剪映草稿所引用的源素材区间抽出的新帧，不要相信上游匹配报告，只根据图片判断。

每个脚本段的帧依次排列：parts=1 的段有 mid/end 两帧；parts>1 的段是一句话拆成几段连续动作画面，按顺序给每段中点 p1、p2…，再加最后一段的 end，要连起来当一个完整动作判断。共 {len(claims)} 段、{frame_count} 张图。S 编号只在本批次内使用，汇总时必须按批次映射回真实语义段编号。
{mapping}

对每个 S 判断画面与文案的匹配等级：
1. DIRECT：画面直接出现文案要求的对象、动作或结果；
2. ACCEPTABLE_DEGRADED：没有直接动作/结果，但画面是与该卖点对应的产品部位或材质特写，且没有明显矛盾。上游 degraded=true 时，相关产品特写可以按此等级承接；这表示“可接受降级”，不表示它证明了功能效果；
3. MISMATCH：画面与文案无关，或出现相反/冲突事实；
4. INSUFFICIENT_EVIDENCE：画面可能相关，但遮挡、分辨率、时序或证据不足，无法可靠判断。

卖点动作/结果优先直接匹配；只有泛包装、远景、静态陈列不能证明瞬吸、干爽、透气、反渗、柔软等效果。对已标记 degraded=true 的语义段，相关的产品/对应部位特写不因缺少直接动作被判为 MISMATCH。品牌、包装、联名、数量、赠品和 CTA 按对应画面相关性判断。黑帧、空帧、完全看不到产品、明显无关画面仍为 MISMATCH。不要因为上游 expected_shot 或 gate 状态自动通过。

只输出一个 JSON 对象：{{"response_version":"{RESPONSE_VERSION}","segments":[{{"index":1,"match_level":"DIRECT|ACCEPTABLE_DEGRADED|MISMATCH|INSUFFICIENT_EVIDENCE","visual_ok":true,"observed":"看到的具体事实","reason":"这些事实为什么满足或不满足本句","confidence":0.8}}],"overall":"PASS|PASS_WITH_DEGRADED|FAIL|UNCERTIFIED"}}。每段必须且只能有一条，index 是从 1 开始且只对应本批次的整数，不得重复、漏段或输出其他批次编号。所有字段必填，observed/reason 不得为空。confidence 必须为有限数值且在 0 到 1 内；零置信度不得给 DIRECT/ACCEPTABLE_DEGRADED，无法判断时应给 INSUFFICIENT_EVIDENCE。ACCEPTABLE_DEGRADED 和 DIRECT 的 visual_ok 都填 true；MISMATCH 和 INSUFFICIENT_EVIDENCE 填 false。不得只给布尔值代替匹配等级。"""


def _parse_model(text: str) -> dict[str, Any]:
    if is_blank(text):
        raise EmptyResponseError("visual QC model returned an empty response")
    payloads: list[Any] = list(iter_json_values(text))

    if payloads:
        # Preserve negative judgments even in legacy/malformed envelopes.
        # Formatting recovery is useful for diagnosis, never certification.
        rows_by_index: dict[int, dict[str, Any]] = {}
        overall_values: list[str] = []
        versions: list[Any] = []
        errors: list[str] = []
        recognized = 0
        for payload in payloads:
            if isinstance(payload, dict):
                rows = payload.get("segments")
                if isinstance(rows, list):
                    recognized += 1
                    versions.append(payload.get("response_version"))
                    overall = str(payload.get("overall") or "").strip().upper()
                    if overall:
                        overall_values.append(overall)
                elif str(payload.get("index") or "").isdigit():
                    recognized += 1
                    errors.append("RESPONSE_ENVELOPE_MISSING")
                    rows = [payload]
                else:
                    recognized += 1
                    errors.append("SEGMENTS_NOT_LIST")
                    if str(payload.get("overall") or "").strip().upper() == "FAIL":
                        overall_values.append("FAIL")
                    rows = []
            elif isinstance(payload, list):
                recognized += 1
                errors.append("RESPONSE_ENVELOPE_MISSING")
                rows = payload
            else:
                errors.append("RESPONSE_NOT_OBJECT")
                rows = []
            for row in rows:
                if isinstance(row, dict) and _normalize_match_level(row) == "MISMATCH":
                    overall_values.append("FAIL")
                if not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] < 1:
                    errors.append("SEGMENT_INDEX_INVALID")
                    continue
                index = row["index"]
                previous = rows_by_index.get(index)
                if previous is not None:
                    errors.append(f"DUPLICATE_SEGMENT:{index}")
                    if previous != row:
                        errors.append(f"CONFLICTING_SEGMENT:{index}")
                    if _normalize_match_level(row) != "MISMATCH":
                        continue
                rows_by_index[index] = row
        if recognized:
            if recognized != 1:
                errors.append("MULTIPLE_RESPONSE_ENVELOPES")
            unknown = [value for value in overall_values if value not in {
                "PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS", "FAIL", "UNCERTIFIED"}]
            overall = "FAIL" if "FAIL" in overall_values else (
                "UNCERTIFIED" if "UNCERTIFIED" in overall_values or unknown else
                "PASS_WITH_GAPS" if "PASS_WITH_GAPS" in overall_values else
                "PASS_WITH_DEGRADED" if "PASS_WITH_DEGRADED" in overall_values else
                ("PASS" if overall_values else ""))
            return {"segments": [rows_by_index[i] for i in sorted(rows_by_index)],
                    "overall": overall,
                    "response_version": versions[0] if len(versions) == 1 else None,
                    "contract_errors": sorted(set(errors))}
    raise ValueError("visual QC response is not JSON")


def _call_qc_model(vision_analyzer: Any, sheet: pathlib.Path, prompt: str,
                   response: pathlib.Path, *, timeout: int,
                   workdir: pathlib.Path, strict_slots: bool = False) -> tuple[bool, str]:
    """单批次视觉验收调用。空回复重试一次，仍为空就按调用失败处理（判 UNCERTIFIED，绝不 PASS）。"""
    ok, text = False, ""
    for _ in range(2):
        ok, text = vision_analyzer._run_vision_with_retry(
            sheet, prompt, response,
            profile=os.environ.get("JY_VISION_PROFILE", "volc").strip() or "volc",
            timeout=timeout, workdir=workdir, schema=None, max_retries=0,
            **({"require_slot": True} if strict_slots else {}),
        )
        empty = (ok and is_blank(text)) or (not ok and vision_analyzer.is_empty_failure(text))
        if not empty:
            return ok, text
    return False, "POST_DRAFT_VISION_EMPTY_RESPONSE: " + str(text or "")[-400:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--draft", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    draft = pathlib.Path(args.draft)
    report_dir = pathlib.Path(args.report_dir)
    out_path = pathlib.Path(args.output)
    result: dict[str, Any] = {
        "version": REPORT_VERSION,
        "draft": str(draft),
        "status": "UNCERTIFIED",
        "checks": [],
        "issues": [],
        "warnings": [],
        "uncertified": False,
        "degraded_matches": [],
        "batch_results": [],
        "preview_only_reasons": [],
        "response_contract": {"version": RESPONSE_VERSION, "status": "NOT_VALIDATED",
                              "validated_batches": [], "errors": []},
    }
    try:
        result["draft_metadata_sha256"] = draft_metadata_fingerprint(draft)
        info = _load_json(draft)
        claims = _expected_claims(report_dir)
        ai_mode = any(c.get("selection_mode") == "ai_editor" for c in claims)
        response_version = RESPONSE_VERSION
        if ai_mode:
            from orchestrator import ai_editor_qc, ai_editor_contract
            result.update(version=ai_editor_qc.REPORT_VERSION, selection_mode="ai_editor")
            response_version = ai_editor_qc.RESPONSE_VERSION
            result["response_contract"]["version"] = response_version
            match_data = json.loads((report_dir / "shot_match_report.json").read_text(encoding="utf-8-sig"))
            result["selection_sha256"] = ai_editor_contract.digest(match_data.get("segments", []))
            identity = json.loads((draft / "ai_selection_identity.json").read_text(encoding="utf-8-sig"))
            if identity.get("selection_sha256") != result["selection_sha256"]:
                raise ValueError("AI_SELECTION_DRAFT_IDENTITY_CHANGED")
            if any(c.get("selection_mode") != "ai_editor" for c in claims):
                raise ValueError("MIXED_SELECTION_MODES")
        result["preview_only_reasons"] = [
            f"UPSTREAM_PREVIEW:S{claim['index']:02d}" for claim in claims if claim["degraded"]]
        active_claims = [claim for claim in claims if not claim["visual_missing"]]
        intentional_gaps = [claim["index"] for claim in claims if claim["visual_missing"]]
        rows, issues = _timeline_items(info, draft)
        active_rows = [row for row in rows if not row.get("intentional_gap")]
        placeholder_rows = [row for row in rows if row.get("intentional_gap")]
        result["timeline"] = {
            "duration_us": info.get("duration"),
            "fps": info.get("fps"),
            "canvas": info.get("canvas_config"),
            "video_segments": len(rows),
            "active_video_segments": len(active_rows),
            "placeholder_segments": len(placeholder_rows),
            "distinct_materials": len({r["material_id"] for r in active_rows}),
            "intentional_visual_gaps": intentional_gaps,
        }
        expected_rows = sum(claim["slots"] for claim in active_claims)
        result["timeline"]["expected_active_video_segments"] = expected_rows
        if not claims:
            issues.append("MATCH_REPORT_MISSING_OR_EMPTY")
        elif len(active_rows) != expected_rows:
            issues.append(f"SEGMENT_COUNT_MISMATCH:draft_active={len(active_rows)},expected_active={expected_rows},active_match={len(active_claims)},intentional_gaps={len(intentional_gaps)}")
        if not ai_mode and len(active_claims) > 1 and len({r["material_id"] for r in active_rows}) < 2:
            issues.append("SINGLE_SOURCE_FOR_MULTI_SEGMENT_DRAFT")
        # 物理片段 → 原句：按时间线顺序，每句吃掉自己的 slots 个片段。数量对不上时不配对，免得错配。
        claim_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        if claims and len(active_rows) == expected_rows:
            cursor = 0
            for claim in active_claims:
                claim_rows.append((claim, active_rows[cursor:cursor + claim["slots"]]))
                cursor += claim["slots"]
        if ai_mode:
            cut_errors = [error for claim, cut_rows in claim_rows
                          for error in ai_editor_qc.validate_cut(claim, cut_rows)]
            issues.extend(cut_errors)
            result["final_windows_verified"] = bool(claim_rows) and not cut_errors
            result["uncertified"] = result["uncertified"] or bool(cut_errors)
            result["source_bindings"] = [{"path": row["source"], "sha256": ai_editor_contract.file_sha256(row["source"])}
                                         for _, cut_rows in claim_rows for row in cut_rows]
        result["segment_map"] = [
            {"claim_index": claim["index"], "draft_segments": [row["index"] for row in rows]}
            for claim, rows in claim_rows]
        for claim in active_claims:
            rule = claim["scene_rule"]
            if rule.get("status") not in ("unmet", "partial"):
                continue
            code = str(rule.get("reason") or "")
            line = f"SCENE_RULE_UNMET:S{claim['index']:02d}:{rule.get('id') or ''}:{code}"
            if code.startswith("SCENE_ACTION_"):
                result["preview_only_reasons"].append(line)
            else:
                result["warnings"].append(line)
                result["uncertified"] = True
        if len(placeholder_rows) != len(intentional_gaps):
            issues.append(f"PLACEHOLDER_COUNT_MISMATCH:draft={len(placeholder_rows)},match={len(intentional_gaps)}")
        result["issues"].extend(issues)
        result["checks"].append({"name": "draft_structure", "status": "FAIL" if issues else "PASS"})

        ffmpeg = _ffmpeg()
        frame_dir = report_dir / "draft_visual_qc_frames"
        frame_items: list[dict[str, Any]] = []
        frame_issues: list[str] = []
        if not ffmpeg:
            frame_issues.append("FFMPEG_MISSING")
        claim_frames: dict[int, list[dict[str, Any]]] = {}
        expected_frames = 0
        for claim, rows in claim_rows:
            if ai_mode:
                plan = ai_editor_qc.frame_plan(claim, rows)
            elif len(rows) == 1:
                plan = [(rows[0], "mid", 0.5), (rows[0], "end", 0.9)]
            else:
                plan = [(row, f"p{n}", 0.5) for n, row in enumerate(rows, 1)]
                plan.append((rows[-1], "end", 0.9))
            expected_frames += len(plan)
            frames = claim_frames.setdefault(claim["index"], [])
            for row, suffix, fraction in plan:
                if not ffmpeg or not _is_file(pathlib.Path(row["source"])):
                    continue
                seconds = row["source_start"] + row["source_duration"] * fraction
                frame = frame_dir / f"segment_{row['index']:03d}_{suffix}.jpg"
                ok, error = _extract(ffmpeg, pathlib.Path(row["source"]), seconds, frame)
                if not ok:
                    frame_issues.append(f"FRAME_EXTRACT_FAILED:S{row['index']:02d}:{suffix}:{error}")
                    continue
                item = {
                    "path": str(frame), "label": f"S{claim['index']:02d} {suffix}",
                    "shot_id": claim["shot_id"], "source_start": row["source_start"],
                    "source_end": row["source_start"] + row["source_duration"],
                    "suffix": suffix, "timestamp": seconds, "draft_segment": row["index"],
                }
                frames.append(item)
                frame_items.append(item)
        result["issues"].extend(frame_issues)
        result["checks"].append({"name": "fresh_source_frames", "status": "FAIL" if frame_issues else "PASS", "count": len(frame_items)})
        if frame_issues or not claim_rows or len(frame_items) < max(2, expected_frames):
            result["warnings"].append("FRESH_FRAME_EVIDENCE_INCOMPLETE")
            result["uncertified"] = True
        else:
            try:
                from orchestrator import vision_analyzer
            except ModuleNotFoundError:
                # Direct execution puts .../orchestrator on sys.path, not its
                # package parent.  Add the skill root so this helper works
                # both as `python -m` and as a file path from the worker.
                sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
                from orchestrator import vision_analyzer
            sheet = report_dir / "draft_visual_qc_contact_sheet.jpg"
            if not vision_analyzer._write_sheet(frame_items, sheet, columns=6, tile_width=260, tile_height=190, ffmpeg=ffmpeg):
                result["issues"].append("CONTACT_SHEET_FAILED")
                result["uncertified"] = True
            else:
                # Keep the overall sheet as evidence, but send bounded
                # semantic batches so one large prompt cannot block all QC.
                batch_size = max(1, int(os.environ.get("JY_POST_QC_BATCH_CLAIMS", "2" if ai_mode else "4")))
                timeout = int(os.environ.get("JY_POST_QC_TIMEOUT_S", "180"))
                visual_rows = []
                batch_count = (len(active_claims) + batch_size - 1) // batch_size
                for batch_no, start in enumerate(range(0, len(active_claims), batch_size), 1):
                    claims = active_claims[start:start + batch_size]
                    items = [dict(item, label=(f"S{local:02d} {item['suffix']} @{item['timestamp']:.3f}s" if ai_mode else f"S{local:02d} {item['suffix']}"))
                             for local, claim in enumerate(claims, 1)
                             for item in claim_frames.get(claim["index"], [])]
                    chunk_sheet = report_dir / f"draft_visual_qc_contact_sheet_{batch_no:02d}.jpg"
                    # Each claim contributes two frames (mid/end), or one
                    # per action part plus the final end frame.
                    # With a one-claim batch, four columns would leave two
                    # black padding tiles; the vision reviewer can mistake
                    # those empty cells for a black end frame.  Keep the
                    # sheet dense so every visible tile is evidence.
                    chunk_columns = min(4, max(2, len(items)))
                    if not vision_analyzer._write_sheet(items, chunk_sheet, columns=chunk_columns,
                                                        tile_width=420 if ai_mode else 260, tile_height=300 if ai_mode else 190,
                                                        ffmpeg=ffmpeg):
                        result["warnings"].append(f"CONTACT_SHEET_FAILED:B{batch_no:02d}")
                        result["uncertified"] = True
                        continue
                    response = report_dir / f"draft_visual_qc_vision_{batch_no:02d}.json"
                    batch_meta = report_dir / f"draft_visual_qc_batch_{batch_no:02d}.json"
                    batch_meta.write_text(json.dumps({
                        "batch_no": batch_no,
                        "claim_indices": [claim["index"] for claim in claims],
                        "frame_labels": [item["label"] for item in items],
                        "prompt_version": response_version,
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    ok, text = _call_qc_model(vision_analyzer, chunk_sheet,
                                              _prompt(claims, len(items)), response,
                                              timeout=timeout, workdir=report_dir, strict_slots=ai_mode)
                    if not ok:
                        result["response_contract"]["errors"].append(f"B{batch_no:02d}:CALL_FAILED")
                        result["batch_results"].append({
                            "batch_no": batch_no,
                            "claim_indices": [claim["index"] for claim in claims],
                            "status": "FAILED",
                            "error": text[-500:],
                        })
                        result["warnings"].append(
                            f"POST_DRAFT_VISION_CALL_FAILED:B{batch_no:02d}:" + text[-500:])
                        result["uncertified"] = True
                        continue
                    try:
                        model = _parse_model(text)
                        contract_errors = response_errors(model, len(claims), expected_version=response_version)
                        if contract_errors:
                            result["response_contract"]["errors"].extend(
                                f"B{batch_no:02d}:{error}" for error in contract_errors)
                            result["warnings"].append(
                                f"POST_DRAFT_VISION_CONTRACT_INVALID:B{batch_no:02d}:" + ";".join(contract_errors))
                            result["uncertified"] = True
                        else:
                            result["response_contract"]["validated_batches"].append(batch_no)
                        overall = model.get("overall")
                        if overall == "FAIL":
                            result["issues"].append(f"POST_DRAFT_VISUAL_MISMATCH:B{batch_no:02d}:MODEL_OVERALL_FAIL")
                        elif overall == "UNCERTIFIED":
                            result["warnings"].append(f"POST_DRAFT_VISION_UNCERTIFIED:B{batch_no:02d}")
                            result["uncertified"] = True
                        elif overall in {"PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}:
                            result["preview_only_reasons"].append(f"MODEL_PREVIEW:B{batch_no:02d}:{overall}")
                        claim_indices = [claim["index"] for claim in claims]
                        model_rows: dict[int, dict[str, Any]] = {}
                        for raw_row in model.get("segments") or []:
                            if not isinstance(raw_row, dict):
                                continue
                            local_index = int(raw_row.get("index")) if str(raw_row.get("index") or "").isdigit() else 0
                            if 1 <= local_index <= len(claim_indices):
                                model_rows[claim_indices[local_index - 1]] = raw_row
                        result["batch_results"].append({
                            "batch_no": batch_no,
                            "claim_indices": claim_indices,
                            "status": "PARSED",
                            "contract_status": "INVALID" if contract_errors else "VALIDATED",
                            "contract_errors": contract_errors,
                            "returned_local_indices": sorted(
                                int(x.get("index")) for x in model.get("segments") or []
                                if isinstance(x, dict) and str(x.get("index") or "").isdigit()
                            ),
                        })
                        for claim in claims:
                            raw_row = model_rows.get(claim["index"])
                            if not raw_row:
                                result["warnings"].append(f"POST_DRAFT_VISION_MISSING:S{claim['index']:02d}")
                                result["uncertified"] = True
                                continue
                            row = dict(raw_row)
                            row["batch_no"] = batch_no
                            row["batch_index"] = int(raw_row.get("index"))
                            row["index"] = claim["index"]
                            row["claim"] = claim["claim"]
                            row["selection_mode"] = claim["selection_mode"]
                            row["degraded_expected"] = claim["degraded"]
                            row["match_level"] = _normalize_match_level(row)
                            row["visual_ok"] = row["match_level"] in {"DIRECT", "ACCEPTABLE_DEGRADED"}
                            visual_rows.append(row)
                            if row["match_level"] == "ACCEPTABLE_DEGRADED":
                                result["degraded_matches"].append({
                                    "index": claim["index"],
                                    "claim": claim["claim"],
                                    "batch_no": batch_no,
                                    "reason": row.get("reason", ""),
                                })
                            elif row["match_level"] == "MISMATCH":
                                result["issues"].append(f"POST_DRAFT_VISUAL_MISMATCH:S{claim['index']:02d}:{row.get('reason','')}")
                            elif row["match_level"] == "INSUFFICIENT_EVIDENCE":
                                result["warnings"].append(f"POST_DRAFT_VISUAL_INSUFFICIENT:S{claim['index']:02d}:{row.get('reason','')}")
                                result["uncertified"] = True
                    except (ValueError, TypeError, AttributeError) as exc:
                        result["response_contract"]["errors"].append(f"B{batch_no:02d}:JSON_INVALID")
                        result["warnings"].append("POST_DRAFT_VISION_JSON_INVALID:" + str(exc))
                        result["uncertified"] = True
                result["model_review"] = visual_rows
                if (not result["response_contract"]["errors"] and
                        result["response_contract"]["validated_batches"] == list(range(1, batch_count + 1))):
                    result["response_contract"]["status"] = "VALIDATED"
                result["checks"].append({
                    "name": "independent_visual_match",
                    "status": "FAIL" if any(str(x).startswith("POST_DRAFT_VISUAL_MISMATCH")
                                               for x in result["issues"]) else
                              "UNCERTIFIED" if result["uncertified"] else "PASS",
                    "batches": batch_count,
                    "batch_size_claims": batch_size,
                })
        result["draft_metadata_sha256_final"] = draft_metadata_fingerprint(draft)
        if ai_mode and any(ai_editor_contract.file_sha256(b["path"]) != b["sha256"] for b in result["source_bindings"]):
            result["issues"].append("AI_QC_SOURCE_CHANGED_DURING_REVIEW")
            result["uncertified"] = True
        if result["draft_metadata_sha256_final"] != result["draft_metadata_sha256"]:
            result["issues"].append("DRAFT_METADATA_CHANGED_DURING_QC")
            result["uncertified"] = True
        degraded_count = len(result.get("degraded_matches") or [])
        result["match_grade_counts"] = {
            "degraded": degraded_count,
            "mismatch": sum(1 for issue in result["issues"] if str(issue).startswith("POST_DRAFT_VISUAL_MISMATCH")),
        }
        result["material_gap_count"] = len(intentional_gaps)
        result["material_gap_status"] = "HAS_GAPS" if intentional_gaps else "CLEAR"
        result["visual_match_status"] = (
            "FAIL" if any(_hard_issue(issue) for issue in result["issues"]) else
            "UNCERTIFIED" if result["uncertified"] else
            "PASS_WITH_DEGRADED" if degraded_count or result["preview_only_reasons"] else "PASS"
        )
        if any(_hard_issue(issue) for issue in result["issues"]):
            result["status"] = "FAIL"
        elif result["uncertified"]:
            result["status"] = "UNCERTIFIED"
        elif intentional_gaps:
            result["status"] = "PASS_WITH_GAPS"
        elif degraded_count or result["preview_only_reasons"]:
            result["status"] = "PASS_WITH_DEGRADED"
        else:
            result["status"] = "PASS"
        result["release_eligibility"] = (
            "SHIP_ALLOWED" if result["status"] == "PASS"
            else "PREVIEW_ONLY" if result["status"] in {"PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}
            else "BLOCKED"
        )
    except Exception as exc:  # noqa: BLE001
        result["status"] = "UNCERTIFIED"
        result["uncertified"] = True
        result["issues"].append("POST_DRAFT_QC_EXCEPTION:" + str(exc))
    if "release_eligibility" not in result:
        result["release_eligibility"] = "BLOCKED"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "issues": result["issues"][-8:], "output": str(out_path)}, ensure_ascii=False))
    return 0 if result["status"] in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
