"""命令行入口：环境自检 / 音色与 BGM 推荐 / 扫描 / 生成草稿。"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import (
    bgm_selector,
    capacity,
    rulepack,
    scene_rules,
    script_polish,
    semantic_gate,
    shot_analyzer,
    shot_notes,
    timing_contract,
    usage_ledger,
    voice_catalog,
    voice_tts,
)
from .engine import OrchestrationEngine
from .platform_env import clear_environment_cache, environment_report
from .task_log import TaskAlreadyRunningError, TaskLogger


def _read(path: Path) -> dict:
    # Accept UTF-8 with or without BOM, as Windows PowerShell 5's
    # `Set-Content -Encoding UTF8` emits a BOM by default.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _emit(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return code


def _compact_build_result(result: dict) -> dict:
    """Keep chat output minimal; full evidence remains in task logs."""
    status = result.get("status")
    if status == "AUDIO_SELECTION_REQUIRED":
        # The chat host renders preview_file entries as playable attachments.
        # Keep stdout deliberately minimal so no implementation details leak
        # into the operator's selection conversation.
        return {
            "voice_auditions": result.get("voice_auditions", []),
            "bgm_auditions": result.get("bgm_auditions", []),
            "reply_format": result.get("reply_format", "音色 <序号>，BGM <序号>"),
        }
    if status != "SUCCESS":
        keys = ("status", "terminal_state", "failure_class", "failure_code",
                "retryable", "safe_to_continue", "next_action", "run_id",
                "pipeline_version", "message", "problems", "reason", "error", "missing",
                "voice_input", "voice_candidates", "voice_auditions", "bgm_candidates", "bgm_auditions",
                "pending_items", "coverage", "report", "confirmation", "task_id", "task_log",
                "issues", "caption_report")
        return {k: result[k] for k in keys if k in result}
    draft_path = result.get("draft_path")
    artifacts = []
    if draft_path:
        p = Path(str(draft_path))
        if p.is_dir():
            for name in ("draft_info.json", "draft_meta_info.json", "draft_settings", "key_value.json"):
                if (p / name).is_file():
                    artifacts.append(name)
            media_count = sum(1 for child in (p / "media").iterdir() if child.is_file()) if (p / "media").is_dir() else 0
            if media_count:
                artifacts.append(f"media/ ({media_count} files)")
    # This exact surface is the operator-facing success contract. Durations,
    # chosen settings, warnings and all diagnostic structure remain in
    # result.json/events.jsonl and must not be rendered into the conversation.
    payload = {
        "草稿名称": result.get("draft_name", ""),
        "草稿路径": str(draft_path or ""),
        "草稿文件": artifacts,
        "生成日志": result.get("task_log", ""),
    }
    # 唯一允许进成功合约的例外：**这份草稿不是正式交付**（定案第 9 条）。
    # 预览稿和有未认证理由的稿子，打开来跟正式稿一模一样，运营无从分辨 ——
    # 只看草稿文件是看不出来的，所以必须写在结果里。形状与已有合约一致：
    # 正式且已认证时这几个键**完全不出现**，老运营侧读法不受影响。
    coverage = result.get("coverage")
    if isinstance(coverage, dict) and coverage:
        if result.get("delivery_mode") == "preview":
            payload["交付模式"] = "preview（预览稿，不得用于正式交付）"
        cert = str(coverage.get("certification") or "").strip().upper()
        reasons = coverage.get("uncertified_reasons")
        if (cert and cert != "CERTIFIED") or reasons:
            payload["认证状态"] = cert or "UNCERTIFIED"
        if reasons:
            payload["未认证原因"] = reasons
    # 画面备注没对上任何一句时不生效，运营要看得见；全部对上时这个键不出现。
    if result.get("shot_notes_unmatched"):
        payload["未绑定画面备注"] = result["shot_notes_unmatched"]
    return payload


def _compact_control_result(result: dict) -> dict:
    """Keep recovery/preflight stdout actionable; retain detail in receipts."""
    keys = (
        "status", "terminal_state", "failure_class", "failure_code", "retryable",
        "safe_to_continue", "next_action", "task_id", "run_id", "pipeline_version",
        "parent_run_id", "recovery", "recovery_stage", "message", "error", "missing",
        "analysis", "matched_manifest", "matched_manifest_sha256", "draft_path", "task_log",
    )
    return {key: result[key] for key in keys if key in result}


def cmd_env(_args) -> int:
    return _emit(environment_report())


def cmd_setup_ffmpeg(args) -> int:
    """公司禁装 ffmpeg 时，自动下载绿色静态构建到 bin/ 并复验（不安装、不写注册表）。"""
    from .ffmpeg_bootstrap import bootstrap_ffmpeg
    with contextlib.redirect_stdout(sys.stderr):
        result = bootstrap_ffmpeg(force=bool(getattr(args, "force", False)))
    # A successful bootstrap changes the paths discovered by the cached report.
    # Invalidate even after a failed attempt so a manually supplied binary is visible.
    clear_environment_cache()
    code = 0 if result.get("status") in ("READY", "ALREADY_READY") else 1
    return _emit(result, code)


def cmd_list_voices(_args) -> int:
    return _emit({"voices": voice_catalog.list_voices()})


def cmd_recommend_voice(args) -> int:
    rec = voice_catalog.recommend_voices(args.text or "", style=args.style or "",
                                         persona=args.persona or "", topk=args.topk)
    return _emit({"recommended": rec[0] if rec else None, "alternatives": rec,
                  "hint": "首选为自动匹配；如需更换，把 voice 设为候选 key 后重新生成"})


def cmd_audition(args) -> int:
    """按脚本/剧情动态推荐 Top N（默认5）并用固定样词生成试听，先听再选。"""
    keys = [k.strip() for k in args.keys.split(",") if k.strip()] if args.keys else None
    out_dir = Path(args.out_dir).expanduser().resolve()
    samples = voice_tts.generate_audition(
        script=args.script or "", out_dir=out_dir, topn=args.topn,
        gender=args.gender or None, keys=keys, all_voices=args.all)
    ok = all(s.get("ok") for s in samples)
    return _emit({"hint": "按 order 逐个播放试听，选定后把该 key(speaker id) 填到 manifest 的 voice；都不合适可加大 --topn(上限10)、加 --gender，或让助手从全库再找",
                  "samples": samples}, 0 if samples and ok else 1)


def cmd_review_script(args) -> int:
    """口播确认单：逐句字数/预估时长/能否塞进画面 + 衔接覆盖问题。"""
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    eng = OrchestrationEngine(manifest, input_path.parent, Path(args.report_dir).resolve())
    full = str(manifest.get("full_script", ""))
    segs = manifest.get("segments", [])
    with contextlib.redirect_stdout(sys.stderr):
        if segs:
            texts, avails = [], []
            for s in segs:
                texts.append(str(s.get("text", s.get("caption", ""))))
                avail = 0.0
                if s.get("video"):
                    try:
                        dur = eng.probe(eng.resolve(s["video"]))["duration"]
                        avail = max(dur - float(s.get("source_start", s.get("in", 0.0))), 0.0)
                    except Exception:
                        avail = 0.0
                avails.append(avail)
        else:
            texts = [str(c) for c in manifest.get("captions", [])]
            avails = None
    cps = float(manifest.get("speech_cps", 4.6))
    conf = script_polish.build_confirmation(full, texts, avails, cps=cps)
    visual_segments = segs if segs else [{"text": t} for t in texts]
    visual = semantic_gate.evaluate_segments(
        visual_segments,
        require_explicit=bool(manifest.get("visual_evidence_required", True)),
    )
    if segs:
        # review-script performs the same source-frame/evidence-interval gate
        # as build, before any media upload or TTS.  Final-window coverage is
        # added by build after stability and audio-driven trimming.
        try:
            eng._raw_segments = segs
            eng.visual_evidence_gate(eng.build_clips())
            visual = getattr(eng, "_visual_evidence_report", visual)
        except Exception as exc:  # review must remain machine-readable
            visual = {**visual,
                      "ok": False,
                      "issues": list(visual.get("issues", [])) + [{
                          "type": "visual_evidence_review_error",
                          "message": str(exc),
                      }]}
    conf["visual_evidence"] = visual
    report_dir = Path(args.report_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "visual_evidence_report.json").write_text(
        json.dumps(visual, ensure_ascii=False, indent=2), encoding="utf-8")
    code = 0 if conf["review"]["ok"] and visual["ok"] and all(
        r["fit"] in ("ok", "unknown") for r in conf["per_sentence"]) else 1
    return _emit(conf, code)


def cmd_list_library(args) -> int:
    """浏览剪映全量音色库（默认164），支持性别/关键词筛选。"""
    from . import voice_library as vl
    voices = vl.load_library()
    gender = {"男": "male", "女": "female", "童": "child"}.get(args.gender or "", args.gender or None)
    rows = []
    for v in voices:
        if gender and v.gender != gender:
            continue
        if args.kw and args.kw not in v.name and args.kw not in v.speaker_id:
            continue
        rows.append(v.to_dict())
    return _emit({"total_library": len(voices), "shown": len(rows), "voices": rows})


def cmd_recommend_bgm(args) -> int:
    cands = bgm_selector.select_bgm(args.mood or "", music_id=args.id or "",
                                    topk=args.topk, min_duration=args.min_dur,
                                    script=args.script or "")
    moods = cands[0]["moods"] if cands else bgm_selector.detect_bgm_moods(args.script or "")
    return _emit({"detected_moods": moods, "candidates": cands,
                  "hint": "基于脚本/剧情对616首曲库相关性排序取Top；选定后把 bgm 设为 {\"music_id\": ...} 或 {\"path\": 本地文件}，无直链的需在剪映内同步后指定 path"})


def cmd_scan(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    eng = OrchestrationEngine(manifest, input_path.parent,
                              Path(args.report_dir).resolve())
    with contextlib.redirect_stdout(sys.stderr):  # 引擎/底层 print 导到 stderr，stdout 只留 JSON
        err = eng.self_check()
        if err:
            return _emit(err, 1)
        meta = {"videos": [], "audios": []}
        clips = eng.build_clips()
        seen_v, seen_a = set(), set()
        for clip in clips:
            if clip.video not in seen_v:
                seen_v.add(clip.video)
                meta["videos"].append(eng.probe(clip.video))
            if clip.audio and clip.audio not in seen_a:
                seen_a.add(clip.audio)
                meta["audios"].append(eng.probe(clip.audio))
    Path(args.report_dir).mkdir(parents=True, exist_ok=True)
    out = Path(args.report_dir) / "asset_metadata.json"
    out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return _emit({"status": "SCANNED", "metadata": str(out)})


def _run_shot_analysis(manifest: dict, input_path: Path, report_dir: Path) -> dict:
    """Create or consume task-local shot candidates; never writes a library."""
    report_dir.mkdir(parents=True, exist_ok=True)
    analysis_file = manifest.get("shot_analysis_file")
    inline = manifest.get("shot_analysis")
    analysis = dict(inline) if isinstance(inline, dict) and isinstance(inline.get("shots"), list) else None
    if analysis_file:
        path = Path(str(analysis_file)).expanduser()
        path = (path if path.is_absolute() else input_path.parent / path).resolve()
        try:
            analysis = _read(path)
        except (OSError, ValueError) as exc:
            return {"ok": False, "pending_items": [{"type": "SHOT_ANALYSIS_FILE_INVALID", "message": str(exc)}]}
        # 复用的候选一律交给 enrich 判定：输入签名对不上的整条重做，动作或文字复核
        # 没做完的只补那一步；全部有效时 enrich 不调模型直接返回。
        if not bool(manifest.get("vision_analysis", True)):
            return analysis
    env = environment_report(refresh=True)
    if analysis is None:
        from . import material_cache
        return material_cache.analyze_product(
            manifest, input_path.parent, report_dir, ffmpeg=env.get("ffmpeg"),
            ffprobe=env.get("ffprobe"), vision=bool(manifest.get("vision_analysis", True)))
    if not bool(manifest.get("vision_analysis", True)):
        return analysis
    from . import vision_analyzer
    return vision_analyzer.enrich_with_vision(manifest, analysis, report_dir,
                                              ffmpeg=env.get("ffmpeg"))


_PROBE_CACHE: dict = {}


def _audio_probe():
    """返回 ffprobe 时长探测函数；没有 ffprobe 时返回 None（调用方显式降级）。

    和引擎 ``align_audio_video`` 用同一个探测口：音频文件只有容器时长，
    引擎会减一帧。这里不对齐的话，场景规则按长一帧的配音切好的窗口
    （定速、证据卡满整段）到引擎里会差一帧放不下，退回素材开头。
    """
    if "probe" in _PROBE_CACHE:
        return _PROBE_CACHE["probe"]
    from .platform_env import find_ffmpeg
    ffmpeg, ffprobe = find_ffmpeg()[:2]
    if not ffprobe:
        _PROBE_CACHE["probe"] = None
        return None

    def probe(path: str) -> dict:
        res = timing_contract.probe_material_duration_us(str(path), ffprobe=str(ffprobe),
                                                         ffmpeg=str(ffmpeg) if ffmpeg else None)
        if int(res.get("duration_us") or 0) <= 0:
            raise RuntimeError(f"ffprobe 读取失败：{res.get('source')}")
        return {"duration": int(res["duration_us"]) / timing_contract.US}

    _PROBE_CACHE["probe"] = probe
    return probe


def _apply_operator_voice_input(manifest: dict) -> dict:
    """Choose one voice input: an explicit manifest value wins over the env override."""
    out = dict(manifest)
    voice = out.get("voice")
    if voice is None or (isinstance(voice, str) and not voice.strip()):
        env_voice = os.environ.get("JY_OPERATOR_VOICE", "").strip()
        if env_voice:
            out["voice"] = env_voice
    return out


def _lock_voice_once(manifest: dict) -> tuple[object | None, dict | None]:
    """全流程唯一的音色决议（用户 2026-09-16 定案第 1 条）。

    manifest 已有 ``voice`` → 直接解析使用；为空 → 只调**一次**现有推荐器并把
    结果回写锁定到 manifest。解析失败**不静默换音色**，返回
    ``VOICE_SELECTION_REQUIRED`` 候选清单交运营选择。
    """
    manifest = _apply_operator_voice_input(manifest)
    sentences = timing_contract.split_sentences(manifest)
    script = " ".join(timing_contract.sentence_text(s) for s in sentences).strip()
    try:
        preset = voice_catalog.resolve_voice_once(
            None, script=script, manifest=manifest, persist=True)
    except voice_catalog.VoiceResolutionError as exc:
        detail = getattr(exc, "voice_detail", None)
        detail = detail if isinstance(detail, dict) else {}
        return None, {
            "status": "VOICE_SELECTION_REQUIRED",
            "message": str(exc),
            "voice_input": detail.get("voice_input") or str(manifest.get("voice", "")),
            "voice_candidates": detail.get("candidates", []),
        }
    return preset, None


def _pre_synthesize_narration(manifest: dict, report_dir: Path, preset) -> dict:
    """选镜**之前**拿到逐句真实配音时长（定案第 3 条）。

    切句口径与规划层共用 `timing_contract.split_sentences`（否则真值表的键对不
    上段落）；合成结果落 `narration_timing.json`，引擎 `synthesize` 因为缓存名
    只含「文本+音色+后端+后处理版本」会命中同一批文件，**不额外花钱**。

    单句合成失败不阻断：该句显式降级为 ``timing_certainty="estimated"``，
    并随段落一路带进成片报告，绝不在正常路径上冒充真值。
    """
    sentences = timing_contract.split_sentences(manifest)
    rows: list[dict] = []
    tts_items: list[dict] = []
    for i, s in enumerate(sentences, 1):
        key = timing_contract.sentence_key(s, i)
        text = timing_contract.sentence_text(s)
        seg = s if isinstance(s, dict) else {}
        declared_audio = str(seg.get("audio") or "").strip()
        declared_us = int(seg.get("audio_duration_us") or 0)
        rows.append({
            "key": key, "text": text, "tts_path": declared_audio,
            "audio_duration_us": declared_us,
            "timing_certainty": str(seg.get("timing_certainty") or ""),
        })
        # 已带音频文件/已声明时长的段落不再合成：段内参数先于本次合成。
        if text and not declared_audio and declared_us <= 0:
            tts_items.append({"key": key, "text": text})

    paths: dict[str, str] = {}
    tts_error = ""
    if tts_items:
        try:
            _, paths = voice_tts.generate_batch(
                tts_items, report_dir / "tts", voice_spec=preset, concurrency=4,
                tempo=manifest.get("voice_tempo"),
                edge_trim=bool(manifest.get("voice_edge_trim", True)))
        except Exception as exc:  # noqa: BLE001 —— 合成失败只降级，不能拦住选镜
            tts_error = f"{type(exc).__name__}: {exc}"
    for row in rows:
        resolved = paths.get(row["key"])
        if resolved:
            row["tts_path"] = resolved

    timing = timing_contract.narration_timing(rows, ffprobe=_audio_probe(), manifest=manifest)
    timing["tts_error"] = tts_error
    timing["tts_attempted"] = len(tts_items)
    timing["voice"] = str(manifest.get("voice", ""))
    (report_dir / "narration_timing.json").write_text(
        json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8")
    return timing


def _validate_preserved_match(manifest: dict, analysis: dict, prior: dict,
                              refill_indices: list[int], narration_timing: dict | None) -> str:
    """Reject stale or unbound selections before reusing an existing cut."""
    import math
    from . import ai_editor_contract
    rows = prior.get("segments") or []
    sentences = timing_contract.split_sentences(manifest)
    if (not isinstance(rows, list) or len(rows) != len(sentences)
            or ai_editor_contract.digest(rows) != ai_editor_contract.digest((prior.get("manifest") or {}).get("segments"))):
        return "PRIOR_SELECTION_IDENTITY_MISMATCH"
    candidates = {str(s.get("shot_id")): s for s in analysis.get("shots", []) if isinstance(s, dict) and s.get("shot_id")}
    for index, (row, sentence) in enumerate(zip(rows, sentences), 1):
        if (not isinstance(row, dict) or int(row.get("index") or index) != index
                or "".join(str(row.get("text") or "").split()) != "".join(timing_contract.sentence_text(sentence).split())):
            return "PRIOR_SENTENCE_MISMATCH"
        if index in refill_indices:
            continue
        if narration_timing:
            duration, _ = timing_contract.duration_for(narration_timing, index, segment=sentence)
            old_duration = row.get("audio_duration_us") or round(float(row.get("segment_s") or 0) * 1000000)
            if not old_duration or abs(int(old_duration) - int(duration)) > 1000:
                return "PRIOR_AUDIO_TIMING_MISMATCH"
        if ai_editor_contract.is_ai_segment(row):
            checked = ai_editor_contract.validate_segment_receipt(row, index=index, verify_source=True, manifest=manifest)
            if not checked.get("ok"):
                return "PRIOR_AI_RECEIPT_INVALID"
        for part in row.get("span_parts") or [row]:
            candidate = candidates.get(str(part.get("shot_id") or part.get("temporary_shot_id") or ""))
            if not candidate or candidate.get("status") != "ready_for_matching" or str(part.get("video")) != str(candidate.get("video")):
                return "PRIOR_SOURCE_CATALOG_MISMATCH"
            try:
                start, end = float(part["source_start"]), float(part["source_end"])
                lower, upper = float(candidate["source_start"]), float(candidate["source_end"])
                if (not all(math.isfinite(n) for n in (start, end, lower, upper))
                        or not Path(str(part["video"])).is_file() or start < lower - 0.001
                        or end > upper + 0.001 or end <= start):
                    return "PRIOR_SOURCE_WINDOW_INVALID"
            except (KeyError, ValueError, TypeError, OSError):
                return "PRIOR_SOURCE_WINDOW_INVALID"
    return ""


def _temporary_match(manifest: dict, input_path: Path, report_dir: Path,
                     narration_timing: dict | None = None) -> tuple[dict, dict | None]:
    from . import ai_editor, ai_editor_contract
    mode = ai_editor_contract.selection_mode(manifest)
    manifest = {**manifest, "selection_mode": mode}
    analysis = _run_shot_analysis(manifest, input_path, report_dir)
    if analysis.get("shots"):
        from . import material_corrections
        analysis = material_corrections.apply(manifest, analysis, report_dir)
    if not analysis.get("shots"):
        return manifest, {"status": "SHOT_MATCH_BLOCKED", "message": "当前商品没有可分析的素材镜头。",
                          "pending_items": analysis.get("pending_items", []),
                          "report": str(report_dir / "shot_candidates.json")}
    from . import vision_analyzer
    vision_meta = analysis.get("vision_analysis")
    vision_meta = vision_meta if isinstance(vision_meta, dict) else {}
    refill_indices = []
    for raw in (manifest.get("ai_refill_indices") or []):
        try:
            refill_indices.append(int(raw))
        except (TypeError, ValueError):
            continue
    refill_indices = sorted(set(i for i in refill_indices if i >= 1))

    if mode == "ai_editor":
        analysis = ai_editor.prepare(manifest, analysis, report_dir, narration_timing=narration_timing)
        matched = shot_analyzer.match_manifest(
            manifest, analysis, topk=int(manifest.get("shot_topk", 3) or 3),
            auto_accept_top1=bool(manifest.get("auto_accept_top1", True)),
            narration_timing=narration_timing)
    elif mode == "rule_then_ai":
        from . import ai_editor_contract
        # Keep prior AI-refill sentences across rebuilds. A fresh rule pass would
        # otherwise overwrite them with degraded_fallback and trip FORMAL_EVIDENCE.
        prior_ai: dict[int, dict] = {}
        prior_match: dict = {}
        prior_path = report_dir / "shot_match_report.json"
        if refill_indices and prior_path.is_file():
            try:
                prior_match = json.loads(prior_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError):
                prior_match = {}
            for i, seg in enumerate(prior_match.get("segments") or [], 1):
                if not isinstance(seg, dict):
                    continue
                try:
                    key = int(seg.get("index") or i)
                except (TypeError, ValueError):
                    key = i
                if ai_editor_contract.is_ai_segment(seg) or str(seg.get("selection_mode") or "") == "ai_editor":
                    prior_ai[key] = seg
        # 1) 规则/描述表初排（走 legacy matcher，不做整库 inventory）
        # Freeze the same clause boundaries used for pre-TTS before switching
        # the matcher to legacy mode. Otherwise its legacy splitter merges
        # clauses, misaligns audio durations and loses sentence-level cuts.
        rule_manifest = {**manifest, "selection_mode": "legacy",
                         "segments": timing_contract.split_sentences(manifest)}
        matched = shot_analyzer.match_manifest(
            rule_manifest, analysis, topk=int(manifest.get("shot_topk", 8) or 8),
            auto_accept_top1=bool(manifest.get("auto_accept_top1", True)),
            narration_timing=narration_timing)
        if matched.get("ok"):
            scene_report = scene_rules.apply(manifest, matched, analysis, report_dir)
            matched["scene_rules"] = {k: scene_report.get(k) for k in
                                      ("category", "rulepacks", "versions", "applied", "partial", "unmet")}
        # A completed prior cut is the baseline for an explicit sentence repair.
        # Re-running the rule matcher can otherwise move unrelated windows and
        # reserve the one candidate needed by the requested sentence.
        if refill_indices and prior_match.get("ok") is True:
            try:
                reason = _validate_preserved_match(manifest, analysis, prior_match, refill_indices, narration_timing)
            except (ValueError, TypeError, KeyError):
                reason = "PRIOR_SELECTION_IDENTITY_MISMATCH"
            if reason:
                return manifest, {"status": "SHOT_MATCH_BLOCKED", "message": reason,
                                  "match_report": str(prior_path)}
            matched = dict(prior_match)
            matched["manifest"] = {**(prior_match.get("manifest") or {}), **manifest,
                                   "segments": prior_match["segments"]}
            prior_ai = {}  # All prior rows are already preserved, including rule rows.
        # Restore prior AI segments that are not being re-refilled this pass.
        if prior_ai:
            keep = set(prior_ai) - set(refill_indices)
            segs = list(matched.get("segments") or [])
            for i, seg in enumerate(segs):
                if not isinstance(seg, dict):
                    continue
                try:
                    key = int(seg.get("index") or i + 1)
                except (TypeError, ValueError):
                    key = i + 1
                if key in keep:
                    segs[i] = dict(prior_ai[key])
            matched["segments"] = segs
            def _drop_kept(rows: object) -> list:
                out = []
                for row in rows or []:
                    if not isinstance(row, dict):
                        out.append(row)
                        continue
                    try:
                        idx = int(row.get("index"))
                    except (TypeError, ValueError):
                        out.append(row)
                        continue
                    if idx not in keep:
                        out.append(row)
                return out
            for key in ("material_gaps", "degraded_matches", "visual_missing_matches",
                        "pending_items", "preview_unresolved"):
                if key in matched:
                    matched[key] = _drop_kept(matched.get(key))
            matched["degraded_count"] = len(matched.get("degraded_matches") or [])
            matched["material_gap_count"] = len(matched.get("material_gaps") or [])
            matched["visual_missing_count"] = len(matched.get("visual_missing_matches") or [])
            if not matched.get("pending_items") and not matched.get("material_gaps") and not matched.get("degraded_matches"):
                matched["ok"] = True
        # 2) QC 指定句再 AI 看图补选；规则初排若已整体失败，才对缺口句补选以图出稿。
        gap_indices = []
        for key in ("material_gaps", "degraded_matches", "visual_missing_matches",
                    "pending_items"):
            for row in matched.get(key) or []:
                if not isinstance(row, dict):
                    continue
                try:
                    gap_indices.append(int(row.get("index")))
                except (TypeError, ValueError):
                    continue
        # A legacy recommendation can be structurally successful while its
        # per-segment claim gate is blocked. Those rows also require pixel
        # review; never let a top-level ok hide an uncovered selling point.
        rule_segments = matched.get("segments") or (matched.get("manifest") or {}).get("segments") or []
        for position, segment in enumerate(rule_segments, 1):
            if not isinstance(segment, dict):
                continue
            gate = segment.get("claim_gate")
            if isinstance(gate, dict) and (gate.get("ok") is False or gate.get("status") == "blocked"):
                try:
                    gap_indices.append(int(segment.get("index") or position))
                except (TypeError, ValueError):
                    gap_indices.append(position)
        need_ai = list(refill_indices)
        if not need_ai and gap_indices:
            # Preview can be structurally "ok" while still containing degraded
            # or missing evidence. Give the configured small set a pixel review.
            need_ai = [i for i in gap_indices if i >= 1]
        need_ai = sorted(set(need_ai))
        if not refill_indices:
            try:
                refill_limit = max(0, int(manifest.get("ai_refill_max_sentences", 0) or 0))
            except (TypeError, ValueError):
                refill_limit = 0
            if refill_limit:
                from . import demo_actions
                sentence_rows = timing_contract.split_sentences(manifest)
                def _gap_priority(index: int) -> tuple[int, int, int]:
                    if not 1 <= index <= len(sentence_rows):
                        return (0, 0, index)
                    text = timing_contract.sentence_text(sentence_rows[index - 1])
                    category = str(manifest.get("category") or "")
                    claims = demo_actions.requirements_for(text, text, category)
                    sentence = demo_actions.sentence_contract(text, category=category,
                                                               product=str(manifest.get("product") or ""))
                    return (-int(sentence.get("kind") == "functional"),
                            -sum(bool(claim.evidence_required) for claim in claims), index)
                need_ai = sorted(need_ai, key=_gap_priority)[:refill_limit]
        rule_selected_count = len(matched.get("segments") or [])
        if need_ai:
            matched = ai_editor.refill_indices(
                manifest, analysis, matched, need_ai, report_dir,
                narration_timing=narration_timing)
        matched["rule_selection_summary"] = {
            "rule_segments": rule_selected_count,
            "automatic_gap_indices": sorted(set(gap_indices)),
            "ai_refill_requested": need_ai,
            "ai_refill_replaced": list(matched.get("refill_replaced") or []),
        }
        matched["selection_mode"] = "rule_then_ai"
        if isinstance(matched.get("manifest"), dict):
            matched["manifest"]["selection_mode"] = "rule_then_ai"
        # 把规则结果写回当前 mode，供引擎消费
        manifest = {**manifest, **(matched.get("manifest") or {}), "selection_mode": "rule_then_ai"}
        if matched.get("segments"):
            manifest["segments"] = matched["segments"]
    elif not analysis.get("semantic_matches") and vision_meta.get("errors") == []:
        analysis = vision_analyzer.match_with_codex(manifest, analysis, report_dir)
        if analysis.get("semantic_match_error"):
            # Rule-only matching cannot judge whether a shot shows what the
            # sentence says; it fills with packaging/product-display shots.
            return manifest, {"status": "SHOT_MATCH_BLOCKED",
                              "message": "VISION_CALL_FAILED: AI 逐句选镜调用失败，已停止，"
                                         "不用规则选镜兜底出稿。",
                              "error": analysis["semantic_match_error"],
                              "report": str(report_dir / "shot_match_ai.json")}
        matched = shot_analyzer.match_manifest(
            manifest, analysis, topk=int(manifest.get("shot_topk", 3) or 3),
            auto_accept_top1=bool(manifest.get("auto_accept_top1", True)),
            narration_timing=narration_timing)
        if matched.get("ok"):
            scene_report = scene_rules.apply(manifest, matched, analysis, report_dir)
            matched["scene_rules"] = {k: scene_report.get(k) for k in
                                      ("category", "rulepacks", "versions", "applied", "partial", "unmet")}
    else:
        matched = shot_analyzer.match_manifest(
            manifest, analysis, topk=int(manifest.get("shot_topk", 3) or 3),
            auto_accept_top1=bool(manifest.get("auto_accept_top1", True)),
            narration_timing=narration_timing)
        if matched.get("ok") and mode != "ai_editor":
            scene_report = scene_rules.apply(manifest, matched, analysis, report_dir)
            matched["scene_rules"] = {k: scene_report.get(k) for k in
                                      ("category", "rulepacks", "versions", "applied", "partial", "unmet")}
    (report_dir / "shot_match_report.json").write_text(
        json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8")
    # DEMO_ACTIONS V0.2 §7（用户定案）：素材缺口单独落盘，交付前必须可见 ——
    # 不允许只躺在 shot_match_report.json 里等人翻。
    if matched.get("contract_issues"):
        (report_dir / "contract_issues.json").write_text(
            json.dumps({"count": matched.get("contract_issue_count", 0),
                        "issues": matched["contract_issues"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    if matched.get("material_gaps"):
        (report_dir / "material_gaps.json").write_text(
            json.dumps({
                "count": matched.get("material_gap_count", 0),
                "claims": matched.get("material_gap_claims", []),
                "gaps": matched["material_gaps"],
                "gate_coverage": matched.get("claim_gate_coverage", {}),
                "policy": matched.get("claim_gate_policy", {}),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not matched.get("ok"):
        return manifest, {"status": "SHOT_MATCH_BLOCKED",
                          "message": "当前商品素材已切成候选镜头，但逐句匹配仍缺少可审计的画面描述、卖点证据或动作完成确认。",
                          "pending_items": matched.get("pending_items", []),
                          "recommendations": matched.get("recommendations", []),
                          "analysis_report": str(report_dir / "shot_candidates.json"),
                          "match_report": str(report_dir / "shot_match_report.json"),
                          "analysis_prompt": str(report_dir / "shot_analysis_prompt.md")}
    return matched["manifest"], None


def cmd_build(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    trigger_phrase = getattr(args, "trigger_phrase", "")
    if trigger_phrase:
        manifest = dict(manifest)
        manifest["mode"] = "intelligent"
        manifest["trigger_phrase"] = trigger_phrase
    report_dir = Path(args.report_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    # 交付模式（定案第 9 条）必须**在选镜之前**就写进 manifest：`_temporary_match`
    # 里的 `match_manifest` 会读它决定「窗口无解时是报缺口还是先出占位画面」。
    # 引擎构造之后才注入就晚了 —— 那时整句已经被判成 SHOT_TIMING_INFEASIBLE，
    # 草稿层根本没有机会出图。这里是唯一入口，引擎侧只做归一化与读取。
    mode = str(getattr(args, "delivery_mode", "") or "formal").strip().lower()
    manifest = _apply_operator_voice_input(manifest)
    manifest["delivery_mode"] = "preview" if mode == "preview" else "formal"
    # 品类规则（转场、配音倍速、句间气口）同样要在 TTS 与选镜之前定下来。
    try:
        manifest = rulepack.apply_manifest_defaults(manifest)
    except (ValueError, TypeError) as exc:
        return _emit({"status": "VOICE_INPUT_INVALID", "terminal_state": "BLOCKED",
                      "failure_class": "environment", "retryable": False,
                      "safe_to_continue": False, "message": str(exc)}, 1)
    ledger = usage_ledger.ledger_path(manifest, report_dir)
    if ledger is not None and not manifest.get("usage_ledger_path"):
        manifest["usage_ledger_path"] = str(ledger)
    try:
        logger = TaskLogger(report_dir, manifest,
                            parent_run_id=str(manifest.get("parent_run_id") or "").strip() or None)
    except TaskAlreadyRunningError as exc:
        return _emit({
            "status": "TASK_ALREADY_RUNNING",
            "terminal_state": "BLOCKED",
            "failure_class": "environment",
            "failure_code": "TASK_ALREADY_RUNNING",
            "retryable": False,
            "safe_to_continue": False,
            "next_action": "manual_review",
            "message": str(exc),
        }, 1)
    shot_match_report = None

    def finalize(result: dict) -> dict:
        if shot_match_report and shot_match_report.is_file():
            result.setdefault("shot_match_report", str(shot_match_report))
        if manifest.get("shot_notes_unmatched"):
            result.setdefault("shot_notes_unmatched", list(manifest["shot_notes_unmatched"]))
        return logger.finalize(result)

    # Do the cheap, deterministic checks before TTS or vision work.  A missing
    # source or unusable runtime must stop this run and never become a generic
    # retryable model failure.
    missing_sources = [str(path) for path in _manifest_sources(manifest, input_path.parent)
                       if not path.is_file()]
    if missing_sources:
        result = finalize({
            "status": "ENV_ERROR",
            "failure_class": "environment",
            "message": "输入素材不存在，未开始视觉分析或写稿。",
            "missing": missing_sources,
        })
        return _emit(_compact_build_result(result), 1)
    env = environment_report(refresh=True)
    if not env.get("ready"):
        result = finalize({
            "status": "ENV_ERROR",
            "failure_class": "environment",
            "message": "运行环境预检未通过，未开始视觉分析或写稿。",
            "problems": [
                *(["缺少 jianying-editor wrapper"] if not env.get("wrapper_available") else []),
                *(["缺少 pyJianYingDraft vendor"] if not env.get("pyjianyingdraft_vendor") else []),
                *(["缺少 ffmpeg/ffprobe"] if not (env.get("ffmpeg") and env.get("ffprobe")) else []),
                *(["剪映草稿目录不可用"] if not env.get("drafts_root_exists") or env.get("drafts_root_access_issues") else []),
                *(["当前外部 Skill 包未通过校验"]
                  if not (env.get("active_package") or {}).get("active_external_package") else []),
                *list((env.get("python_deps") or {}).get("missing_required") or []),
            ],
            "environment": env,
        })
        return _emit(_compact_build_result(result), 1)

    # Freeze one verified text plan before narration, matching and note binding.
    # Existing segment/audio mappings and explicit notes are never reindexed.
    from . import ai_editor_contract, sentence_planner
    if (not manifest.get("segments") and not shot_notes._notes_of(manifest)
            and not manifest.get("shot_notes_by_index")
            and (manifest.get("temporary_material_analysis") or manifest.get("auto_match_shots"))
            and ai_editor_contract.selection_mode(manifest) in ("ai_editor", "rule_then_ai")
            and manifest.get("semantic_sentence_planning", True)):
        try:
            manifest = sentence_planner.plan(manifest, report_dir / "sentence_planning")
            (report_dir / "sentence_planning" / "planned_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except (ValueError, TypeError, OSError) as exc:
            return _emit(_compact_build_result(finalize({
                "status": "SENTENCE_PLAN_REVIEW_REQUIRED", "terminal_state": "BLOCKED",
                "failure_class": "semantic", "retryable": True,
                "safe_to_continue": False, "next_action": "manual_review",
                "message": "语义分句失败或未完整保留原文，未开始配音和选镜。",
                "error": str(exc),
            })), 1)
    manifest = shot_notes.apply(manifest)

    # 音色先锁一次并回写 manifest：全流程只在这里决议音色，引擎后续只读锁。
    # 必须在 TTS 之前，否则「按哪个音色合成」和「按哪个音色选镜」可能不是同一个。
    locked_voice, voice_blocker = _lock_voice_once(manifest)
    if voice_blocker:
        voice_blocker["report_dir"] = str(report_dir)
        return _emit(_compact_build_result(finalize(voice_blocker)), 1)
    try:
        if manifest.get("temporary_material_analysis") or manifest.get("auto_match_shots"):
            # 选镜之前先跑一遍 TTS 拿逐句真实时长：规划层据此求解窗口，
            # 装不下的镜头在**规划阶段**就被判 SHOT_TIMING_INFEASIBLE，
            # 而不是拖到 write_draft 才 AUDIO_VIDEO_MISMATCH。
            narration_timing = _pre_synthesize_narration(manifest, report_dir, locked_voice)
            if manifest.get("sentence_plan_version") == "semantic-v1":
                manifest["narration_timing_file"] = str(report_dir / "narration_timing.json")
                (report_dir / "sentence_planning" / "planned_manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            manifest, blocker = _temporary_match(manifest, input_path, report_dir,
                                                 narration_timing=narration_timing)
            shot_match_report = report_dir / "shot_match_report.json"
            if blocker:
                blocker["report_dir"] = str(report_dir)
                return _emit(_compact_build_result(finalize(blocker)), 1)
            if manifest.get("usage_ledger_path"):
                try:
                    usage_ledger.record(manifest, manifest.get("segments") or [], reserved=True)
                except (OSError, TimeoutError, ValueError) as exc:
                    logger.event("usage_ledger_reserve_failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.event("prebuild_exception", error_type=type(exc).__name__, error=str(exc))
        result = finalize({
            "status": "ERROR",
            "failure_class": "unknown",
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        return _emit(_compact_build_result(result), 1)

    eng = OrchestrationEngine(manifest, input_path.parent, report_dir,
                              strict_script=args.strict_script, logger=logger)
    try:
        # Engine and dependency chatter belongs to the task log, not the chat.
        # The only terminal payload is _compact_build_result below.
        runtime_log = logger.task_dir / "engine_output.log"
        with runtime_log.open("a", encoding="utf-8") as runtime_out, \
                contextlib.redirect_stdout(runtime_out), \
                contextlib.redirect_stderr(runtime_out):
            result = eng.run()
    except Exception as exc:  # noqa: BLE001
        logger.event("task_exception", error_type=type(exc).__name__, error=str(exc))
        result = {"status": "ERROR", "error": str(exc), "error_type": type(exc).__name__}
    if manifest.get("usage_ledger_path"):
        try:
            if result.get("status") == "SUCCESS":
                result["usage_ledger"] = usage_ledger.record(manifest, manifest.get("segments") or [])
            else:
                usage_ledger.release(manifest)
        except (OSError, TimeoutError, ValueError) as exc:
            logger.event("usage_ledger_failed", error=str(exc))
    if manifest.get("rulepack"):
        result.setdefault("rulepack", manifest["rulepack"])
    result = finalize(result)
    code = 0 if result.get("status") == "SUCCESS" else 1
    return _emit(_compact_build_result(result), code)


def cmd_analyze_shots(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    report_dir = Path(args.report_dir).expanduser().resolve()
    env = environment_report()
    from . import material_cache
    result = material_cache.analyze_product(
        manifest, input_path.parent, report_dir,
        ffmpeg=env.get("ffmpeg"), ffprobe=env.get("ffprobe"),
        threshold=args.threshold, min_shot_s=args.min_shot_s, vision=not args.no_vision)
    result.update({"status": "SHOT_ANALYSIS_READY",
                   "candidate_file": str(report_dir / "shot_candidates.json"),
                   "prompt_file": str(report_dir / "shot_analysis_prompt.md")})
    return _emit(result, 0 if result.get("shots") else 1)


def cmd_match_shots(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    manifest = _read(input_path)
    report_dir = Path(args.report_dir).expanduser().resolve()
    analysis_path = Path(args.analysis).expanduser().resolve()
    try:
        analysis = _read(analysis_path)
    except (OSError, ValueError) as exc:
        return _emit({"status": "SHOT_ANALYSIS_FILE_INVALID", "message": str(exc)}, 1)
    from . import ai_editor, ai_editor_contract
    manifest = {**manifest, "selection_mode": ai_editor_contract.selection_mode(manifest)}
    if manifest["selection_mode"] == "ai_editor":
        analysis = ai_editor.prepare(manifest, analysis, report_dir)
    result = shot_analyzer.match_manifest(manifest, analysis, topk=args.topk,
                                          auto_accept_top1=not args.require_selection)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "shot_match_report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.get("ok"):
        matched_path = report_dir / "matched_manifest.json"
        matched_path.write_text(json.dumps(result["manifest"], ensure_ascii=False, indent=2), encoding="utf-8")
        result["matched_manifest"] = str(matched_path)
    result["status"] = "SHOT_MATCH_READY" if result.get("ok") else "SHOT_MATCH_BLOCKED"
    result["match_report"] = str(report_path)
    return _emit(result, 0 if result.get("ok") else 1)


def cmd_diagnose_timeline(args) -> int:
    from .diagnostics import diagnose_timeline
    result = diagnose_timeline(Path(args.draft), expect_external_package=bool(args.expect_external_package))
    if args.report_dir:
        out = Path(args.report_dir).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        path = out / "timeline_diagnosis.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["report"] = str(path)
    return _emit(result, 0 if result.get("status") == "DIAGNOSIS_OK" else 1)


def cmd_recover(args) -> int:
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        return _emit({"status": "RECOVERY_MANIFEST_REQUIRED", "message": "恢复必须提供完整 manifest 文件。"}, 1)
    manifest = _read(input_path)
    if not (manifest.get("segments") or manifest.get("videos") or
            manifest.get("material_sources")):
        return _emit({"status": "RECOVERY_MANIFEST_REQUIRED",
                      "message": "manifest 缺少 material_sources、segments 或 videos，无法安全恢复。"}, 1)
    stage = str(getattr(args, "stage", "draft_write") or "draft_write").strip().lower()
    parent_run_id = str(manifest.get("parent_run_id") or manifest.get("run_id") or
                        manifest.get("task_id") or "").strip() or None
    manifest = dict(manifest)
    manifest["parent_run_id"] = parent_run_id
    manifest["recovery_stage"] = stage
    suffix = time.strftime("%Y%m%d_%H%M%S")
    manifest["draft_name"] = f"{manifest.get('draft_name') or '豆包剪映编排草稿'}_recovery_{suffix}"
    manifest["force_overwrite"] = False
    report_dir = Path(args.report_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    try:
        logger = TaskLogger(report_dir, manifest,
                            parent_run_id=str(manifest.get("parent_run_id") or "").strip() or None)
    except TaskAlreadyRunningError as exc:
        return _emit({
            "status": "TASK_ALREADY_RUNNING",
            "terminal_state": "BLOCKED",
            "failure_class": "environment",
            "failure_code": "TASK_ALREADY_RUNNING",
            "retryable": False,
            "safe_to_continue": False,
            "next_action": "manual_review",
            "message": str(exc),
        }, 1)

    def finalize(result: dict) -> dict:
        result["recovery"] = True
        result["recovery_stage"] = stage
        return logger.finalize(result)

    if stage == "preflight":
        missing = [str(path) for path in _manifest_sources(manifest, input_path.parent)
                   if not path.is_file()]
        env = environment_report(refresh=True)
        if missing or not env.get("ready"):
            result = finalize({
                "status": "ENV_ERROR",
                "failure_class": "environment",
                "message": "恢复前预检未通过，未进入生成阶段。",
                "missing": missing,
                "environment": env,
            })
            return _emit(_compact_control_result(result), 1)
        result = finalize({
            "status": "PREFLIGHT_READY",
            "message": "恢复前预检通过；尚未执行生成。",
            "environment": env,
        })
        return _emit(_compact_control_result(result), 0)

    if stage == "ui_acceptance":
        draft = str(getattr(args, "draft", "") or "").strip()
        if not draft:
            result = finalize({
                "status": "RECOVERY_DRAFT_REQUIRED",
                "failure_class": "ui",
                "message": "ui_acceptance 阶段必须提供 --draft，恢复不会重新做视觉分析或写稿。",
            })
            return _emit(_compact_control_result(result), 1)
        from .diagnostics import diagnose_timeline
        try:
            diagnosis = diagnose_timeline(Path(draft), expect_external_package=True)
        except Exception as exc:  # noqa: BLE001
            result = finalize({
                "status": "ERROR",
                "failure_class": "ui",
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            return _emit(_compact_control_result(result), 1)
        diagnosis_status = str(diagnosis.get("status") or "DESKTOP_IMPORT_BLOCKED")
        diagnosis_ok = diagnosis_status == "DIAGNOSIS_OK"
        if diagnosis_ok:
            diagnosis.update({
                "status": "UI_ACCEPTANCE_PENDING",
                "message": "草稿诊断已完成；剪映桌面端可见性和完整播放仍需人工确认。",
            })
        else:
            # Preserve a failed diagnostic as a UI blocker.  It is not merely
            # an uncertified preview: the draft/report must stay at this stage
            # until the concrete import/package issue is fixed.
            diagnosis.update({
                "diagnosis_status": diagnosis_status,
                "status": (diagnosis_status if diagnosis_status in {
                    "DESKTOP_IMPORT_BLOCKED", "PACKAGE_NOT_ACTIVE"
                } else "DESKTOP_IMPORT_BLOCKED"),
                "failure_class": "ui",
                "message": "草稿诊断未通过，已停在桌面验收阶段；保留草稿和诊断报告。",
            })
        result = finalize(diagnosis)
        return _emit(_compact_control_result(result), 0 if diagnosis_ok else 1)

    if stage == "semantic_match":
        from . import ai_editor_contract
        timing = None
        if manifest.get("sentence_plan_version") == "semantic-v1":
            timing_path = Path(str(manifest.get("narration_timing_file") or ""))
            try:
                if not timing_path.is_file():
                    raise ValueError("missing timing file")
                timing = _read(timing_path)
                rows = timing.get("rows") or []
                sentences = timing_contract.split_sentences(manifest)
                if len(rows) != len(sentences) or any(
                    row.get("key") != timing_contract.sentence_key(sentence, i)
                    or row.get("text") != timing_contract.sentence_text(sentence)
                    or int(row.get("audio_duration_us") or 0) <= 0
                    for i, (row, sentence) in enumerate(zip(rows, sentences), 1)
                ):
                    raise ValueError("sentence timing mismatch")
            except (OSError, ValueError, TypeError, AttributeError):
                result = finalize({"status": "RECOVERY_SENTENCE_TIMING_REQUIRED",
                                   "failure_class": "semantic",
                                   "message": "语义分句恢复需要与每句对应的原始配音时长文件。"})
                return _emit(_compact_control_result(result), 1)
        elif (not manifest.get("segments") and not shot_notes._notes_of(manifest)
              and not manifest.get("shot_notes_by_index")
              and (manifest.get("temporary_material_analysis") or manifest.get("auto_match_shots"))
              and ai_editor_contract.selection_mode(manifest) in ("ai_editor", "rule_then_ai")
              and manifest.get("semantic_sentence_planning", True)):
            result = finalize({"status": "RECOVERY_SENTENCE_PLAN_REQUIRED",
                               "failure_class": "semantic",
                               "message": "请使用原任务 sentence_planning/planned_manifest.json 恢复，避免分句错位。"})
            return _emit(_compact_control_result(result), 1)
        analysis = str(manifest.get("shot_analysis_file") or "").strip()
        analysis_path = Path(analysis).expanduser() if analysis else report_dir / "shot_candidates.json"
        if not analysis_path.is_absolute():
            analysis_path = input_path.parent / analysis_path
        if not analysis_path.is_file():
            result = finalize({
                "status": "RECOVERY_ANALYSIS_REQUIRED",
                "failure_class": "semantic",
                "message": "语义恢复要求已有 shot_candidates.json；未找到时不会重新扫描整批素材。",
                "analysis": str(analysis_path),
            })
            return _emit(_compact_control_result(result), 1)
        manifest["shot_analysis_file"] = str(analysis_path.resolve())
        try:
            matched_manifest, blocker = _temporary_match(manifest, input_path, report_dir,
                                                         narration_timing=timing)
        except Exception as exc:  # noqa: BLE001
            result = finalize({"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)})
            return _emit(_compact_control_result(result), 1)
        if blocker:
            blocker["report_dir"] = str(report_dir)
            result = finalize(blocker)
            return _emit(_compact_control_result(result), 1)
        matched_path = report_dir / "matched_manifest.json"
        matched_path.write_text(json.dumps(matched_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        result = finalize({
            "status": "SHOT_MATCH_READY",
            "message": "语义匹配已从已有分析恢复；下一步显式执行 draft_write。",
            "matched_manifest": str(matched_path),
        })
        return _emit(_compact_control_result(result), 0)

    if stage != "draft_write":
        result = finalize({"status": "RECOVERY_STAGE_INVALID", "message": f"不支持的恢复阶段：{stage}"})
        return _emit(_compact_control_result(result), 1)

    # draft_write 只消费已存在的匹配结果；没有缓存就明确阻断，避免恢复命令
    # 偷偷重新扫描视觉素材，造成“恢复”实际变成整稿重建。
    cached_path = report_dir / "matched_manifest.json"
    if not cached_path.is_file():
        result = finalize({
            "status": "RECOVERY_MATCHED_MANIFEST_REQUIRED",
            "failure_class": "semantic",
            "message": "draft_write 只接受已有匹配 manifest，不会重新做视觉分析。",
            "matched_manifest": str(cached_path),
        })
        return _emit(_compact_control_result(result), 1)
    try:
        cached_result = _read(cached_path)
    except (OSError, ValueError, TypeError):
        cached_result = {}
    # A raw input manifest can also contain segments.  Require the nested
    # result shape emitted by semantic_match so draft_write cannot silently
    # fall back to the original, unverified segments after a corrupt cache.
    cached = cached_result.get("manifest") if isinstance(cached_result, dict) else None
    if (not isinstance(cached_result, dict) or cached_result.get("ok") is not True
            or not isinstance(cached, dict)
            or not (cached.get("segments") or cached.get("videos"))):
        result = finalize({
            "status": "RECOVERY_MATCHED_MANIFEST_REQUIRED",
            "failure_class": "semantic",
            "message": "matched_manifest.json 缺少已匹配的 segments/videos，未进入写稿。",
            "matched_manifest": str(cached_path),
        })
        return _emit(_compact_control_result(result), 1)
    manifest.update(cached)
    matched_manifest_sha256 = hashlib.sha256(
        json.dumps(cached_result, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    manifest["temporary_material_analysis"] = False
    manifest["auto_match_shots"] = False
    eng = OrchestrationEngine(manifest, input_path.parent, report_dir, logger=logger)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = eng.run()
    except Exception as exc:  # noqa: BLE001
        result = {"status": "ERROR", "error": str(exc), "error_type": type(exc).__name__}
    result["matched_manifest_sha256"] = matched_manifest_sha256
    result = finalize(result)
    return _emit(_compact_control_result(result), 0 if result.get("status") == "SUCCESS" else 1)


def cmd_migrate_encrypted(args) -> int:
    """Hand encrypted drafts to JianYing's official copy/migration flow."""
    from .migration import migrate_encrypted
    result = migrate_encrypted(Path(args.source_draft), args.new_name,
                               wait_seconds=args.wait_seconds)
    return _emit(result, 0 if result.get("status") in {"MIGRATION_VERIFIED", "MIGRATION_NOT_REQUIRED"} else 1)


def _manifest_sources(manifest: dict, base_dir: Path) -> list[Path]:
    if manifest.get("material_sources"):
        values = manifest.get("material_sources", [])
    elif manifest.get("segments"):
        values = [item.get("video") for item in manifest.get("segments", [])
                  if isinstance(item, dict)]
    else:
        values = manifest.get("videos", [])
    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        p = Path(str(value)).expanduser()
        p = (p if p.is_absolute() else base_dir / p).resolve()
        if str(p) not in seen:
            seen.add(str(p)); paths.append(p)
    return paths


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _task_stage_times(task_dir: Path) -> dict:
    events = task_dir / "events.jsonl"
    result: dict[str, float] = {}
    try:
        for line in events.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            event = str(row.get("event", ""))
            if event.endswith("_finish") and "elapsed_s" in row:
                result[event[:-7]] = row["elapsed_s"]
    except (OSError, ValueError, TypeError):
        pass
    return result


def _jianying_ui_probe() -> dict:
    """Read only the Windows process responsiveness flag; never drive the UI."""
    if not sys.platform.startswith("win"):
        return {"checked": False, "reason": "not_windows"}
    script = ("$p=Get-Process JianyingPro,CapCut,VideoFusion -ErrorAction SilentlyContinue;"
              "if($p){$p | Select-Object ProcessName,Id,Responding | ConvertTo-Json -Compress}else{'[]'}")
    try:
        run = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                             text=True, timeout=10, encoding="utf-8", errors="replace")
        data = json.loads((run.stdout or "[]").strip() or "[]")
        rows = data if isinstance(data, list) else [data]
        return {"checked": bool(rows), "processes": rows,
                "all_responding": bool(rows) and all(r.get("Responding") is not False for r in rows)}
    except Exception as exc:  # diagnostics only
        return {"checked": False, "reason": type(exc).__name__}


class _CapacityCalibrationEngine(OrchestrationEngine):
    """Run the calibration's explicitly silent synthetic timeline."""

    def audio_selection_gate(self, clips):
        if (not self.capacity_guard and self.m.get("allow_silent") is True
                and not self.m.get("voice") and not self.m.get("bgm")
                and clips and all(c.audio_mode == "mute" and not c.text for c in clips)):
            return None
        return super().audio_selection_gate(clips)


def cmd_calibrate_capacity(args) -> int:
    """Run isolated incremental source-count calibration and persist the safe limit."""
    input_path = Path(args.input).expanduser().resolve()
    base_manifest = _read(input_path)
    originals = _manifest_sources(base_manifest, input_path.parent)
    missing = [str(p) for p in originals if not p.is_file()]
    if not originals or missing:
        return _emit({"status": "CALIBRATION_INPUT_INVALID", "message": "校准素材缺失，未开始测试。",
                      "missing": missing}, 1)
    test_root = Path(args.test_root).expanduser().resolve()
    report_root = Path(args.report_dir).expanduser().resolve()
    test_id = time.strftime("v131_%Y%m%d_%H%M%S")
    isolated = test_root / test_id
    assets = isolated / "assets"
    reports = report_root / "capacity_calibration" / test_id
    assets.mkdir(parents=True, exist_ok=False)
    reports.mkdir(parents=True, exist_ok=False)
    profile_path = Path(args.profile_path).expanduser().resolve() if args.profile_path else None
    baseline = []
    for path in originals:
        stat = path.stat()
        baseline.append({"path": str(path), "bytes": stat.st_size, "sha256": _sha256(path)})
    host_before = capacity.resource_snapshot(test_root)
    calibration_minimum = capacity.CRITICAL_MEMORY_LIMIT
    if host_before["memory_available_bytes"] and host_before["memory_available_bytes"] < calibration_minimum:
        output = {"status": "SYSTEM_RESOURCE_PRESSURE", "message": "可用内存不足 256 MiB，为避免影响剪映编辑，校准已暂停。",
                  "test_root": str(isolated), "baseline": baseline, "host_before": host_before}
        (reports / "calibration.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        return _emit(output, 1)

    step, maximum = max(1, args.step), max(1, args.max_sources)
    trials, last_passing = [], 0
    for source_count in range(step, maximum + 1, step):
        trial_assets = assets / f"sources_{source_count:03d}"
        trial_assets.mkdir()
        copied: list[Path] = []
        copy_started = time.perf_counter()
        try:
            for idx in range(source_count):
                origin = originals[idx % len(originals)]
                target = trial_assets / f"{idx + 1:03d}_{origin.name}"
                shutil.copy2(origin, target)
                copied.append(target)
        except OSError as exc:
            trials.append({"source_count": source_count, "status": "COPY_FAILED", "error": str(exc),
                           "copy_elapsed_s": round(time.perf_counter() - copy_started, 3)})
            break
        manifest = {
            "draft_name": f"JYSkill_v131_capacity_{test_id}_{source_count:03d}",
            "segments": [{"video": str(path), "audio_mode": "mute", "duration": 0.0} for path in copied],
            "allow_silent": True,
            "stabilize": False,
            "auto_transition": False,
            "execution_profile": "background_friendly",
            "force_overwrite": False,
        }
        manifest_path = isolated / f"manifest_{source_count:03d}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        trial_report = reports / f"trial_{source_count:03d}"
        trial_report.mkdir()
        logger = TaskLogger(trial_report, manifest)
        started = time.perf_counter()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                engine = _CapacityCalibrationEngine(manifest, isolated, trial_report, logger=logger,
                                                    capacity_guard=False)
                result = engine.run()
        except Exception as exc:  # noqa: BLE001
            result = {"status": "ERROR", "error_type": type(exc).__name__, "error": str(exc)}
        result = logger.finalize(result)
        host_after = capacity.resource_snapshot(test_root)
        ui = _jianying_ui_probe()
        trial = {
            "source_count": source_count,
            "copy_elapsed_s": round(time.perf_counter() - copy_started, 3),
            "engine_elapsed_s": round(time.perf_counter() - started, 3),
            "status": result.get("status"), "result": result,
            "stage_elapsed_s": _task_stage_times(logger.task_dir),
            "host_after": host_after, "jianying_ui_probe": ui,
        }
        trials.append(trial)
        pressure = (host_after["memory_available_bytes"] and
                    host_after["memory_available_bytes"] < capacity.CRITICAL_MEMORY_LIMIT)
        ui_failed = ui.get("checked") and not ui.get("all_responding")
        if result.get("status") != "SUCCESS" or pressure or ui_failed:
            break
        last_passing = source_count

    profile = None
    if last_passing:
        # Use the actual drafts drive as fingerprint anchor, not the temporary
        # asset directory, so normal builds use the calibrated profile.
        storage = Path(environment_report().get("drafts_root") or test_root)
        profile = capacity.create_profile(last_passing_count=last_passing, storage_path=storage,
                                          samples=trials)
        saved = capacity.save_profile(profile, profile_path)
    else:
        saved = None
    output = {
        "status": "CAPACITY_CALIBRATED" if profile else "CAPACITY_CALIBRATION_FAILED",
        "test_root": str(isolated), "report_dir": str(reports), "baseline": baseline,
        "host_before": host_before, "trials": trials, "last_passing_count": last_passing,
        "safe_source_limit": profile.get("safe_source_limit") if profile else None,
        "profile_path": str(saved) if saved else None,
        "ui_interaction_note": "SSH 仅检查剪映进程 Responding 状态，未自动模拟 10 轮人工时间线交互。",
    }
    (reports / "calibration.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return _emit(output, 0 if profile else 1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jianying-orchestrator",
                                     description="豆包剪映智能编排引擎（跨平台）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("env", help="环境自检").set_defaults(func=cmd_env)

    sf = sub.add_parser("setup-ffmpeg",
                        help="缺ffmpeg或被公司禁装时，自动下载绿色版到bin并复验（不安装）")
    sf.add_argument("--force", action="store_true", help="已存在也强制重新下载")
    sf.set_defaults(func=cmd_setup_ffmpeg)

    sub.add_parser("list-voices", help="列出可选 TTS 音色").set_defaults(func=cmd_list_voices)

    rv = sub.add_parser("recommend-voice", help="按剧情推荐音色（可二次选择）")
    rv.add_argument("--text", default="", help="脚本文案/剧情内容")
    rv.add_argument("--style", default="")
    rv.add_argument("--persona", default="")
    rv.add_argument("--topk", type=int, default=5)
    rv.set_defaults(func=cmd_recommend_voice)

    au = sub.add_parser("audition", help="按脚本推荐Top音色并用固定样词生成试听")
    au.add_argument("--script", default="", help="脚本/AI剧情文本，用于相关性推荐；空则按通用带货")
    au.add_argument("--topn", type=int, default=5, help="试听条数，默认5、上限10")
    au.add_argument("--gender", default="", help="可选：男/女/童")
    au.add_argument("--keys", default="", help="手动指定音色key/id，逗号分隔，优先于推荐")
    au.add_argument("--all", action="store_true", help="对全库生成（一般不用）")
    au.add_argument("--out-dir", default=".voice_audition")
    au.set_defaults(func=cmd_audition)

    ll = sub.add_parser("list-library", help="浏览剪映全量音色库(约164)，可筛选")
    ll.add_argument("--gender", default="", help="男/女/童")
    ll.add_argument("--kw", default="", help="名称或id关键词，如 解说/妈妈/广告")
    ll.set_defaults(func=cmd_list_library)

    rb = sub.add_parser("recommend-bgm", help="基于脚本/剧情从曲库排序推荐BGM Top5")
    rb.add_argument("--script", "--text", dest="script", default="", help="脚本文案/AI剧情，用于情绪识别与相关性排序")
    rb.add_argument("--mood", default="", help="显式指定情绪(轻快/舒缓/动感/可爱/大气/治愈/国风)，权重更高")
    rb.add_argument("--id", default="")
    rb.add_argument("--min-dur", type=float, default=0.0)
    rb.add_argument("--topk", type=int, default=5)
    rb.set_defaults(func=cmd_recommend_bgm)

    sc = sub.add_parser("scan", help="只扫描素材元数据")
    sc.add_argument("--input", required=True)
    sc.add_argument("--report-dir", default=".jy_run")
    sc.set_defaults(func=cmd_scan)

    sa = sub.add_parser("analyze-shots", help="对当前任务素材分镜抽帧，默认调用视觉模型补充观察")
    sa.add_argument("--input", required=True, help="含 material_sources/videos 的 manifest.json")
    sa.add_argument("--report-dir", default=".jy_run")
    sa.add_argument("--threshold", type=float, default=0.30, help="ffmpeg 场景切换阈值")
    sa.add_argument("--min-shot-s", type=float, default=0.45, help="最短候选镜头秒数")
    sa.add_argument("--no-vision", action="store_true",
                    help="仅生成原始抽帧清单，不调用自动视觉分析（仅诊断用）")
    sa.set_defaults(func=cmd_analyze_shots)

    ms = sub.add_parser("match-shots", help="默认 AI 看图复审并整稿编排；legacy 使用 Top3/Top1")
    ms.add_argument("--input", required=True, help="含 full_script/segments 的 manifest.json")
    ms.add_argument("--analysis", required=True, help="analyze-shots 生成并经 AI 填写描述的 shot_candidates.json")
    ms.add_argument("--report-dir", default=".jy_run")
    ms.add_argument("--topk", type=int, default=3, help="legacy 路线的推荐镜头数")
    ms.add_argument("--require-selection", action="store_true", help=argparse.SUPPRESS)
    ms.set_defaults(func=cmd_match_shots)

    rs = sub.add_parser("review-script", help="口播确认单：逐句字数/预估时长/衔接覆盖体检")
    rs.add_argument("--input", required=True, help="manifest.json（默认口播来自输入，不重写）")
    rs.add_argument("--report-dir", default=".jy_run")
    rs.set_defaults(func=cmd_review_script)

    bd = sub.add_parser("build", help="生成剪映草稿")
    bd.add_argument("--input", required=True, help="manifest.json")
    bd.add_argument("--report-dir", default=".jy_run")
    bd.add_argument("--strict-script", action="store_true", help="文案校验不通过则阻断")
    bd.add_argument("--delivery-mode", choices=("formal", "preview"), default="formal",
                    help="formal（默认）：时序无解即阻断，不得静默通过；"
                         "preview：未解出的段落按素材可用长度出占位画面并在成片上打"
                         "未认证标注，仅供预览，不得用于正式交付。")
    bd.add_argument("--trigger-phrase", default="", help=argparse.SUPPRESS)
    bd.set_defaults(func=cmd_build)

    dt = sub.add_parser("diagnose-timeline", help="只读诊断草稿导入/时间线状态")
    dt.add_argument("--draft", required=True, help="草稿目录")
    dt.add_argument("--report-dir", default=".jy_run")
    dt.add_argument("--expect-external-package", action="store_true",
                    help="核验当前命令是否来自标准豆包 Skills 交付包")
    dt.set_defaults(func=cmd_diagnose_timeline)

    rc = sub.add_parser("recover", help="从完整 manifest 创建不覆盖原稿的恢复草稿")
    rc.add_argument("--input", required=True, help="完整 manifest.json")
    rc.add_argument("--report-dir", default=".jy_run")
    rc.add_argument("--stage", choices=("preflight", "semantic_match", "draft_write", "ui_acceptance"),
                    default="draft_write", help="只恢复一个阶段；默认只消费已有匹配结果写稿")
    rc.add_argument("--draft", default="", help="ui_acceptance 阶段的草稿目录")
    rc.set_defaults(func=cmd_recover)

    me = sub.add_parser("migrate-encrypted", help="通过剪映官方副本流程迁移加密草稿（不解密、不覆盖）")
    me.add_argument("--source-draft", required=True, help="明确的源草稿目录，禁止猜测最近草稿")
    me.add_argument("--new-name", required=True, help="全新的目标草稿名")
    me.add_argument("--wait-seconds", type=int, default=0,
                    help="等待官方副本出现并验证，最多300秒；默认0表示只打开客户端并返回指引")
    me.set_defaults(func=cmd_migrate_encrypted)

    cc = sub.add_parser("calibrate-capacity", help="在隔离目录递增压测并保存本机素材容量上限")
    cc.add_argument("--input", required=True, help="含测试视频的 manifest.json")
    cc.add_argument("--test-root", required=True, help="隔离测试根目录，命令只会在其下创建新目录")
    cc.add_argument("--report-dir", default=".jy_run", help="独立校准报告目录")
    cc.add_argument("--step", type=int, default=13, help="每轮新增素材数，默认 13")
    cc.add_argument("--max-sources", type=int, default=65, help="最多测试的唯一素材数，默认 65")
    cc.add_argument("--profile-path", default="", help="可选的 host_capacity.json 保存位置")
    cc.set_defaults(func=cmd_calibrate_capacity)

    # 兼容旧用法：无子命令时按 build 处理，--analysis 暂接受并忽略（segments 直接内嵌）
    if argv is None:
        argv = sys.argv[1:]
    if argv in (["-h"], ["--help"]):
        parser.print_help()
        return 0
    phrases = {"智能编排", "智能生成", "开启智能模式"}
    if argv and argv[0] in phrases:
        phrase = argv[0]
        if len(argv) == 1:
            return _emit({"status": "INPUT_REQUIRED", "mode": "intelligent",
                          "trigger_phrase": phrase,
                          "message": "请补充本次任务的素材路径和脚本文案后再试。"}, 1)
        argv = ["build", *argv[1:], "--trigger-phrase", phrase]
    if argv and not argv[0].startswith("-") and argv[0] in ("env", "setup-ffmpeg",
        "list-voices",
        "recommend-voice", "audition", "list-library", "recommend-bgm",
        "review-script", "scan", "analyze-shots", "match-shots", "build", "diagnose-timeline", "recover", "migrate-encrypted",
        "calibrate-capacity"):
        args = parser.parse_args(argv)
        return args.func(args)

    # 旧式：--input x [--scan-only]
    legacy = argparse.ArgumentParser(add_help=False)
    legacy.add_argument("--input")
    legacy.add_argument("--report-dir", default=".jy_run")
    legacy.add_argument("--scan-only", action="store_true")
    legacy.add_argument("--analysis")
    known, _ = legacy.parse_known_args(argv)
    if known.input:
        if known.scan_only:
            return cmd_scan(argparse.Namespace(input=known.input,
                                               report_dir=known.report_dir))
        return cmd_build(argparse.Namespace(input=known.input,
                                            report_dir=known.report_dir,
                                            strict_script=False))
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
