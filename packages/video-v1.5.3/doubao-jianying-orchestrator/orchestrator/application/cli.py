"""命令行入口：环境自检 / 音色与 BGM 推荐 / 扫描 / 生成草稿。"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..audio import bgm_selector
from ..runtime import capacity
from .. import rulepack
from ..policy import scene_rules
from ..policy import script_polish
from ..policy import semantic_gate
from ..selection import shot_analyzer
from ..selection import shot_notes
from ..selection import timing_contract
from ..selection import usage_ledger
from ..audio import voice_catalog
from ..audio import voice_tts
from ..audio.library_resources import RuntimeCatalogError
from .engine import OrchestrationEngine
from ..runtime.platform_env import clear_environment_cache, environment_report, find_ffmpeg
from ..runtime.file_safety import content_identity
from ..runtime.task_log import TaskAlreadyRunningError, TaskLogger


def _read(path: Path) -> dict:
    # Accept UTF-8 with or without BOM, as Windows PowerShell 5's
    # `Set-Content -Encoding UTF8` emits a BOM by default.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _content_unchanged(identity: dict) -> bool:
    try:
        return content_identity(identity["path"]) == identity
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _emit(obj: dict, code: int = 0) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return code


def _vision_configuration_gate(runtime: dict) -> dict | None:
    error = runtime.get("vision_configuration_error")
    if not error:
        return None
    return {"status": "ENV_ERROR", "terminal_state": "BLOCKED",
            "failure_class": "environment",
            "retryable": False, "safe_to_continue": False, "next_action": "fix_environment",
            "message": "视觉通道配置不受支持，未开始配音、视觉分析或写稿。",
            "error": error}


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
                "pending_items", "coverage", "report", "confirmation", "task_id", "task_log", "draft_path",
                "issues", "caption_report", "failed_stage", "resume_stage", "runtime")
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
    runtime = result.get("runtime") or {}
    payload["运行信息"] = {key: runtime.get(key) for key in (
        "pipeline_version", "selection_mode", "selection_mode_source",
        "configuration_source", "delivery_mode", "configuration_warnings")}
    for key in ("draft_backend", "fallback_reason"):
        if result.get(key):
            payload["运行信息"][key] = result[key]
    if result.get("delivery_mode") == "preview":
        payload["交付模式"] = "preview（预览稿，不得用于正式交付）"
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
        "analysis", "match_report", "matched_manifest", "matched_manifest_sha256",
        "draft_path", "task_log", "reason", "failed_stage", "resume_stage",
        "previous_match_report", "draft_backend", "fallback_reason",
    )
    return {key: result[key] for key in keys if key in result}


def _production_environment_report(base_report: dict, manifest: dict | None = None, *, require_vision: bool = False) -> dict:
    """Compose writer and audio-resource readiness at the application boundary."""
    from ..runtime.headless import production_environment_report
    from ..audio.bgm_request import normalize
    from ..audio.library_resources import catalog_report
    from ..runtime import vision_config
    report = production_environment_report(base_report, manifest)
    catalogs = catalog_report(require_music=not normalize((manifest or {}).get("bgm")).get("path"))
    report["runtime_catalogs"] = catalogs
    report["hints"].extend(catalogs["errors"])
    report["ready"] = report["ready"] and catalogs["ready"]
    try:
        missing = vision_config.missing_required(manifest)
    except ValueError as exc:
        missing = [str(exc)]
    report["vision_configuration"] = {"required": require_vision, "missing": missing}
    if require_vision and missing:
        report["ready"] = False
        report["hints"].append("VISION_CONFIGURATION_MISSING: " + ", ".join(missing))
    report["status"] = "READY" if report["ready"] else "ENV_ERROR"
    if not report["ready"]:
        report["reason"] = (next(iter(catalogs["errors"]), None) or
                            report["draft_backend_check"].get("reason") or
                            ("VISION_CONFIGURATION_MISSING: " + ", ".join(missing) if require_vision and missing else None) or
                            "BASE_ENVIRONMENT_NOT_READY")
    return report


def _run_command(command, args) -> int:
    try:
        return command(args)
    except RuntimeCatalogError as exc:
        return _emit({"status": "ENV_ERROR", "failure_class": "environment", "reason": str(exc)}, 1)


def cmd_env(args) -> int:
    profile = getattr(args, "profile", "production")
    input_file = getattr(args, "input", None)
    try:
        manifest = _read(Path(input_file).expanduser().resolve()) if input_file else {}
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
    except (OSError, ValueError, TypeError) as exc:
        return _emit({"ready": False, "profile": profile, "status": "ENV_ERROR",
                      "reason": "ENV_MANIFEST_INVALID", "error": str(exc)}, 1)
    report = environment_report(refresh=True)
    if profile == "production":
        report = _production_environment_report(report, manifest, require_vision=True)
    else:
        report = {**report, "profile": "base"}
    return _emit(report, 0 if report.get("ready") else 1)


def cmd_setup_ffmpeg(args) -> int:
    """公司禁装 ffmpeg 时，自动下载绿色静态构建到 bin/ 并复验（不安装、不写注册表）。"""
    from ..media.ffmpeg_bootstrap import bootstrap_ffmpeg
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
    """浏览随包交付的全量音色库，支持性别/关键词筛选。"""
    from ..audio import voice_library as vl
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
                  "hint": "按随包曲库排序；建稿时按 music_id 实时取链下载，bgm 为空则自动选曲，也可指定本地 path。"})


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
    # Keep the exact previous observations only if their digest is the one
    # recorded by the task's old editor plan, before media metadata is refreshed.
    prior = analysis
    if prior is None and (report_dir / "shot_candidates.json").is_file():
        try:
            prior = _read(report_dir / "shot_candidates.json")
        except (OSError, ValueError):
            pass
    if isinstance(prior, dict):
        from ..selection import ai_editor
        ai_editor.snapshot_previous_analysis(prior, report_dir)
    env = environment_report(refresh=True)
    if analysis is None:
        from ..vision import material_cache
        return material_cache.analyze_product(
            manifest, input_path.parent, report_dir, ffmpeg=env.get("ffmpeg"),
            ffprobe=env.get("ffprobe"), vision=bool(manifest.get("vision_analysis", True)))
    from ..vision import material_cache
    try:
        analysis = material_cache.refresh_external_analysis(
            analysis, input_path.parent, ffmpeg=env.get("ffmpeg"), ffprobe=env.get("ffprobe"))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "pending_items": [{"type": "MATERIAL_DURATION_RECHECK_FAILED", "message": str(exc)}]}
    # Existing candidates still go through enrich for source/text validation;
    # duration migration itself never upgrades their visual evidence.
    if not bool(manifest.get("vision_analysis", True)):
        return analysis
    from ..vision import vision_analyzer
    return vision_analyzer.enrich_with_vision(manifest, analysis, report_dir,
                                              ffmpeg=env.get("ffmpeg"))


_PROBE_CACHE: dict = {}


def _audio_probe():
    """返回 ffprobe 时长探测函数；没有 ffprobe 时返回 None（调用方显式降级）。

    与引擎、字幕和最终草稿共享真实音频探测，保留微秒句长。
    视频素材的末帧保护不能用于音频，避免每句少算一帧。
    """
    if "probe" in _PROBE_CACHE:
        return _PROBE_CACHE["probe"]
    from ..runtime.platform_env import find_ffmpeg
    ffmpeg, ffprobe = find_ffmpeg()[:2]
    if not ffprobe:
        _PROBE_CACHE["probe"] = None
        return None

    from ..audio.narration_probe import probe_audio
    def probe(path: str) -> dict:
        return probe_audio(path, ffprobe=str(ffprobe))

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
    except RuntimeCatalogError as exc:
        return None, {"status": "ENV_ERROR", "failure_class": "environment", "reason": str(exc)}
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


from ..audio.narration_preparation import NarrationError


def _pre_synthesize_narration(manifest: dict, report_dir: Path, preset) -> dict:
    """选镜**之前**拿到逐句真实配音时长（定案第 3 条）。

    切句口径与规划层共用 `timing_contract.split_sentences`（否则真值表的键对不
    上段落）；合成结果落 `narration_timing.json`。纸品引擎直接绑定已冻结的最终文件，
    不再调用合成／后处理；其他品类沿用增量缓存，**不额外花钱**。

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

    from ..audio import narration_preparation
    paper = rulepack.for_manifest(manifest).get("category") == "zhipin"
    final_options = {"final_narration": manifest.get("narration_loudness") or {}} if paper else {}
    paths: dict[str, str] = {}
    tts_error = ""
    if tts_items:
        try:
            _, paths = voice_tts.generate_batch(
                tts_items, report_dir / "tts", voice_spec=preset, concurrency=4,
                tempo=manifest.get("voice_tempo"),
                edge_trim=bool(manifest.get("voice_edge_trim", True)), **final_options)
        except Exception as exc:  # noqa: BLE001
            if paper:
                if isinstance(exc, narration_preparation.NarrationError):
                    raise
                raise narration_preparation.NarrationError("NARRATION_PREPARATION_FAILED:" + str(exc),
                    status="NARRATION_PREPARATION_FAILED") from exc
            tts_error = f"{type(exc).__name__}: {exc}"
    for row in rows:
        resolved = paths.get(row["key"])
        if resolved:
            row["tts_path"] = resolved

    if paper:
        retained = {str(Path(p).resolve()) for p in manifest.get("retained_tts_audio", [])}
        fixed = dict(manifest.get("_paper_fixed_narrations") or {})
        ffmpeg, ffprobe = find_ffmpeg()[:2]
        for row in rows:
            path = str(Path(row["tts_path"]).resolve()) if row.get("tts_path") else ""
            if path in retained and (manifest.get("narration_loudness") or {}).get("enabled") is not False:
                # Frozen TTS may receive the existing fixed-length gain treatment,
                # but must never be shortened after a prior selection.
                try:
                    record = narration_preparation.frozen(path, loudness=manifest.get("narration_loudness"))
                    record = record or narration_preparation.prepare(path, report_dir / "fixed-narration",
                        loudness=manifest.get("narration_loudness"), edge_trim=False,
                        ffmpeg=str(ffmpeg or "ffmpeg"), ffprobe=str(ffprobe or "ffprobe"))
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    raise NarrationError("NARRATION_PREPARATION_FAILED:" + str(exc),
                        status="NARRATION_PREPARATION_FAILED") from exc
                fixed[path] = record
                row["tts_path"] = record["path"]
        if fixed:
            manifest["_paper_fixed_narrations"] = fixed
    timing = timing_contract.narration_timing(rows, ffprobe=_audio_probe(), manifest=manifest)
    timing["tts_error"] = tts_error
    timing["tts_attempted"] = len(tts_items)
    timing["voice"] = str(manifest.get("voice", ""))
    (report_dir / "narration_timing.json").write_text(
        json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8")
    if paper:
        narration_preparation.preflight(timing, report_dir, fps=float(manifest.get("fps") or 30),
            max_frames=int((rulepack.for_manifest(manifest).get("voice") or {})["acceptance_max_gap_frames"]),
            ffmpeg=str(find_ffmpeg()[0] or "ffmpeg"))
    return timing


def _validate_preserved_match(manifest: dict, analysis: dict, prior: dict,
                              refill_indices: list[int], narration_timing: dict | None) -> str:
    """Reject stale or unbound selections before reusing an existing cut."""
    import math
    from .. import ai_editor_contract
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


def _invalidate_match_report(report_dir: Path, matching_input: dict | None, failure: dict) -> str | None:
    path = report_dir / "shot_match_report.json"
    backup = None
    try:
        current = _read(path)
    except (OSError, ValueError):
        current = {}
    if (not isinstance(current, dict) or current.get("ok") is not False
            or current.get("input_manifest") != matching_input):
        if path.is_file():
            raw = path.read_bytes()
            if raw:
                archive = report_dir / "match_history"
                archive.mkdir(parents=True, exist_ok=True)
                backup = archive / (hashlib.sha256(raw).hexdigest() + ".json")
                try:
                    with backup.open("xb") as handle:
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                except FileExistsError:
                    if backup.read_bytes() != raw:
                        raise OSError("MATCH_REPORT_ARCHIVE_CONFLICT")
        current = {}
    current.update({"ok": False, "input_manifest": matching_input,
                    "status": failure.get("status", "SHOT_MATCH_BLOCKED"),
                    "message": failure.get("message", "本次匹配未完成，旧成功结果不可用于恢复。")})
    report_dir.mkdir(parents=True, exist_ok=True)
    from ..runtime.file_safety import atomic_write_text
    atomic_write_text(path, json.dumps(current, ensure_ascii=False, indent=2))
    return str(backup) if backup else None


def _temporary_match(manifest: dict, input_path: Path, report_dir: Path,
                     narration_timing: dict | None = None) -> tuple[dict, dict | None]:
    matching_input = None
    try:
        matching_input = _read(input_path) if input_path.is_file() else None
        selected, blocker = _match_temporary_input(manifest, input_path, report_dir,
                                                  narration_timing=narration_timing)
    except BaseException:
        _invalidate_match_report(report_dir, matching_input, {"status": "SHOT_MATCH_BLOCKED"})
        raise
    if blocker:
        _invalidate_match_report(report_dir, matching_input, blocker)
    return selected, blocker


def _match_temporary_input(manifest: dict, input_path: Path, report_dir: Path,
                           narration_timing: dict | None = None) -> tuple[dict, dict | None]:
    matching_input = _read(input_path) if input_path.is_file() else None
    from ..selection import ai_editor
    from .. import ai_editor_contract
    mode = ai_editor_contract.selection_mode(manifest)
    manifest = {**manifest, "selection_mode": mode}
    analysis = _run_shot_analysis(manifest, input_path, report_dir)
    if analysis.get("shots"):
        from ..vision import material_corrections
        analysis = material_corrections.apply(manifest, analysis, report_dir)
    if not analysis.get("shots"):
        return manifest, {"status": "SHOT_MATCH_BLOCKED", "message": "当前商品没有可分析的素材镜头。",
                          "pending_items": analysis.get("pending_items", []),
                          "report": str(report_dir / "shot_candidates.json")}
    from ..vision import vision_analyzer
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
        if refill_indices:
            prior_path = report_dir / "shot_match_report.json"
            try:
                prior_match = json.loads(prior_path.read_text(encoding="utf-8-sig"))
                reason = _validate_preserved_match(manifest, analysis, prior_match, refill_indices, narration_timing)
                if prior_match.get("ok") is not True:
                    reason = "PRIOR_SELECTION_NOT_COMPLETE"
            except (OSError, ValueError, TypeError, KeyError):
                reason = "PRIOR_SELECTION_IDENTITY_MISMATCH"
            if reason:
                return manifest, {"status": "SHOT_MATCH_BLOCKED", "message": reason,
                                  "match_report": str(prior_path)}
            matched = ai_editor.refill_indices(manifest, analysis, prior_match, refill_indices,
                                                report_dir, narration_timing=narration_timing)
        else:
            analysis = ai_editor.prepare(manifest, analysis, report_dir, narration_timing=narration_timing)
            matched = shot_analyzer.match_manifest(
                manifest, analysis, topk=int(manifest.get("shot_topk", 3) or 3),
                auto_accept_top1=bool(manifest.get("auto_accept_top1", True)),
                narration_timing=narration_timing)
    elif mode == "rule_then_ai":
        from .. import ai_editor_contract
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
                from ..policy import demo_actions
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
        analysis = vision_analyzer.match_sentences(manifest, analysis, report_dir)
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
    if matching_input is not None:
        matched["input_manifest"] = matching_input
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
                          **{k: matched[k] for k in ("status", "failure_class", "failure_code", "retryable", "reason", "failed_stage", "resume_stage") if k in matched},
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
    mode = str(getattr(args, "delivery_mode", None) or manifest.get("delivery_mode") or "preview").strip().lower()
    manifest = _apply_operator_voice_input(manifest)
    manifest["delivery_mode"] = mode
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

    if mode not in {"preview", "formal"}:
        return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
            "failure_class": "environment", "reason": "INVALID_DELIVERY_MODE"})), 1)
    if logger.runtime.get("selection_configuration_error"):
        return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
            "failure_class": "environment", "reason": logger.runtime["selection_configuration_error"]})), 1)
    from .. import ai_editor_contract
    from ..policy import playback
    try:
        if (manifest.get("max_source_reuse") is not None and not manifest.get("source_reuse_policy")
                and not os.environ.get("JY_PRESERVE_UNVERSIONED_QUEUE_REUSE")):
            manifest["source_reuse_policy"] = "strict-v1"
        playback.task_absorb_speed_cap(manifest)
        ai_editor_contract.source_reuse_limit(manifest)
    except ValueError as exc:
        return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
            "failure_class": "environment", "reason": str(exc),
            "message": "速度或素材复用设置无效，未开始配音或视觉分析。"})), 1)
    for warning in logger.runtime.get("configuration_warnings") or []:
        print("WARNING: " + warning, file=sys.stderr)

    # Do the cheap, deterministic checks before TTS or vision work.  A missing
    # source or unusable runtime must stop this run and never become a generic
    # retryable model failure.
    configuration_blocker = _vision_configuration_gate(logger.runtime)
    if configuration_blocker:
        return _emit(_compact_build_result(finalize(configuration_blocker)), 1)
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

    draft_env = _production_environment_report(env, manifest,
        require_vision=bool(manifest.get("temporary_material_analysis") or manifest.get("auto_match_shots")))
    if not draft_env["ready"]:
        return _emit(_compact_build_result(finalize({'status': 'ENV_ERROR', 'failure_class': 'environment',
            'message': '建稿环境预检未通过，未开始配音或视觉分析。',
            'reason': draft_env['reason'],
            'environment': draft_env})), 1)

    from .task_inputs import validate_explicit_audio
    try:
        segments = validate_explicit_audio(manifest.get("segments"), input_path.parent,
            ffprobe=str(((draft_env.get("draft_backend_check") or {}).get("media_tools") or {}).get("ffprobe")
                        or env.get("ffprobe") or "ffprobe"))
        if "segments" in manifest:
            manifest["segments"] = segments
        fixed_audio = {i: content_identity(row["audio"])
                       for i, row in enumerate(segments or [], 1)
                       if isinstance(row, dict) and row.get("audio")}
    except (OSError, ValueError, TypeError) as exc:
        reason = str(exc) if str(exc).startswith("FIXED_NARRATION_") else "FIXED_NARRATION_CHANGED"
        return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
            "failure_class": "environment", "reason": reason,
            "message": "指定配音不可读取，未开始视觉分析或重新合成。"})), 1)
    from ..audio import bgm_request
    bgm_identity = None
    try:
        bgm = bgm_request.normalize(manifest.get("bgm"))
        if bgm.get("path"):
            checked_bgm = validate_explicit_audio([{"audio": bgm["path"]}], input_path.parent,
                ffprobe=str(((draft_env.get("draft_backend_check") or {}).get("media_tools") or {}).get("ffprobe")
                            or env.get("ffprobe") or "ffprobe"))
            manifest["bgm"] = {**bgm, "path": checked_bgm[0]["audio"]}
            bgm_identity = content_identity(checked_bgm[0]["audio"])
    except (OSError, ValueError, TypeError):
        return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
            "failure_class": "environment", "reason": "BGM_SOURCE_AUDIO_INVALID",
            "message": "指定背景音乐不可读取，未开始视觉分析或配音。"})), 1)

    # Freeze one verified text plan before narration, matching and note binding.
    # Previously bound notes stay frozen; new natural-sentence notes map from
    # original character spans onto the verified semantic plan.
    from .. import ai_editor_contract
    from ..selection import sentence_planner
    if (not manifest.get("segments") and shot_notes.permits_semantic_planning(manifest)
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
    try:
        manifest = shot_notes.apply(manifest)
    except (ValueError, TypeError) as exc:
        return _emit(_compact_build_result(finalize({"status": "SHOT_NOTES_BINDING_INVALID",
            "failure_class": "semantic", "retryable": False, "reason": str(exc),
            "message": "画面备注无法安全绑定到原文，未开始配音或选镜。"})), 1)

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
            supplied = {i for i, row in enumerate(manifest.get("segments") or [], 1)
                        if isinstance(row, dict) and row.get("audio")}
            verified = {row.get("index") for row in narration_timing.get("rows", [])
                        if row.get("timing_certainty") == "audio" and not row.get("probe_error")}
            if supplied - verified:
                return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
                    "failure_class": "environment", "reason": "FIXED_NARRATION_TIMING_UNAVAILABLE",
                    "message": "指定配音无法确定真实时长，未开始选镜。"})), 1)
            if any(not _content_unchanged(row) for row in fixed_audio.values()):
                return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
                    "failure_class": "environment", "reason": "FIXED_NARRATION_CHANGED",
                    "message": "指定配音在测时期间发生变化，未开始选镜。"})), 1)
            manifest["narration_timing_file"] = str(report_dir / "narration_timing.json")
            if manifest.get("sentence_plan_version") == "semantic-v1":
                # A frozen input skips the planner, including its directory setup.
                (report_dir / "sentence_planning").mkdir(parents=True, exist_ok=True)
                (report_dir / "sentence_planning" / "planned_manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            audio_files = []
            for row in narration_timing.get("rows", []):
                path = Path(str(row.get("tts_path") or ""))
                if path.is_file():
                    audio_files.append(content_identity(path))
            manifest["_selection_attempt"] = logger.run_id
            if not manifest.get("ai_refill_indices"):
                (report_dir / "selection_resume.json").write_text(json.dumps({
                    "input_manifest": _read(input_path), "manifest": manifest,
                    "audio_files": audio_files, "bgm_file": bgm_identity, "timing": narration_timing,
                    "run_id": logger.run_id,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            manifest, blocker = _temporary_match(manifest, input_path, report_dir,
                                                 narration_timing=narration_timing)
            shot_match_report = report_dir / "shot_match_report.json"
            if blocker:
                blocker["report_dir"] = str(report_dir)
                return _emit(_compact_build_result(finalize(blocker)), 1)
            if any(not _content_unchanged(row) for row in audio_files):
                return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
                    "failure_class": "environment", "reason": "SELECTION_AUDIO_CHANGED",
                    "message": "选镜期间配音发生变化，未开始写稿。"})), 1)
            if bgm_identity and not _content_unchanged(bgm_identity):
                return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
                    "failure_class": "environment", "reason": "BGM_CHANGED_DURING_SELECTION",
                    "message": "选镜期间指定音乐发生变化，未开始写稿。"})), 1)
            selected_segments = manifest.get("segments") or []
            for index, identity in fixed_audio.items():
                row = selected_segments[index - 1] if index <= len(selected_segments) else None
                if (not isinstance(row, dict) or not row.get("audio")
                        or str(Path(str(row["audio"])).resolve()) != identity["path"]):
                    return _emit(_compact_build_result(finalize({"status": "ENV_ERROR",
                        "failure_class": "environment", "reason": "FIXED_NARRATION_BINDING_CHANGED:S%d" % index,
                        "message": "选镜结果未保留指定配音，未开始写稿。"})), 1)
            if manifest.get("usage_ledger_path"):
                try:
                    usage_ledger.record(manifest, manifest.get("segments") or [], reserved=True)
                except (OSError, TimeoutError, ValueError) as exc:
                    logger.event("usage_ledger_reserve_failed", error=str(exc))
    except NarrationError as exc:
        return _emit(_compact_build_result(finalize(exc.result())), 1)
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
    except NarrationError as exc:
        result = exc.result()
    except Exception as exc:  # noqa: BLE001
        logger.event("task_exception", error_type=type(exc).__name__, error=str(exc))
        result = {"status": "ERROR", "error": str(exc), "error_type": type(exc).__name__}
    if any(not _content_unchanged(row) for row in fixed_audio.values()):
        result = {**result, "status": "ENV_ERROR", "failure_class": "environment",
                  "reason": "FIXED_NARRATION_CHANGED_DURING_BUILD",
                  "message": "建稿期间指定配音发生变化，保留本地结果供复核，不交付。"}
    elif bgm_identity and not _content_unchanged(bgm_identity):
        result = {**result, "status": "ENV_ERROR", "failure_class": "environment",
                  "reason": "BGM_CHANGED_DURING_BUILD",
                  "message": "建稿期间指定音乐发生变化，保留本地结果供复核，不交付。"}
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
    from ..vision import material_cache
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
    from .. import ai_editor_contract
    from ..policy import playback
    matching_input = copy.deepcopy(manifest)
    report_dir = Path(args.report_dir).expanduser().resolve()
    analysis_path = Path(args.analysis).expanduser().resolve()
    completed = False
    try:
        try:
            if manifest.get("max_source_reuse") is not None and not manifest.get("source_reuse_policy"):
                manifest["source_reuse_policy"] = "strict-v1"
            playback.task_absorb_speed_cap(manifest)
            ai_editor_contract.source_reuse_limit(manifest)
        except ValueError as exc:
            return _emit({"status": "ENV_ERROR", "reason": str(exc),
                          "message": "速度或素材复用设置无效，未开始视觉分析。"}, 1)
        try:
            manifest = shot_notes.apply(manifest)
        except (ValueError, TypeError) as exc:
            return _emit({"status": "SHOT_NOTES_BINDING_INVALID", "reason": str(exc),
                          "message": "画面备注无法安全绑定到原文，未开始选镜。"}, 1)
        try:
            analysis = _read(analysis_path)
        except (OSError, ValueError) as exc:
            return _emit({"status": "SHOT_ANALYSIS_FILE_INVALID", "message": str(exc)}, 1)
        from ..selection import ai_editor
        from .. import ai_editor_contract
        from ..vision import material_cache
        ai_editor.snapshot_previous_analysis(analysis, report_dir)
        env = environment_report(refresh=True)
        try:
            analysis = material_cache.refresh_external_analysis(
                analysis, input_path.parent, ffmpeg=env.get("ffmpeg"), ffprobe=env.get("ffprobe"))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return _emit({"status": "SHOT_MATCH_BLOCKED", "reason": "MATERIAL_DURATION_RECHECK_FAILED",
                          "message": str(exc)}, 1)
        manifest = {**manifest, "selection_mode": ai_editor_contract.selection_mode(manifest)}
        if manifest["selection_mode"] == "ai_editor":
            analysis = ai_editor.prepare(manifest, analysis, report_dir)
        result = shot_analyzer.match_manifest(manifest, analysis, topk=args.topk,
                                              auto_accept_top1=not args.require_selection)
        result["input_manifest"] = matching_input
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "shot_match_report.json"
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.get("ok"):
            matched_path = report_dir / "matched_manifest.json"
            matched_path.write_text(json.dumps(result["manifest"], ensure_ascii=False, indent=2), encoding="utf-8")
            result["matched_manifest"] = str(matched_path)
        result["status"] = "SHOT_MATCH_READY" if result.get("ok") else "SHOT_MATCH_BLOCKED"
        result["match_report"] = str(report_path)
        completed = True
        return _emit(result, 0 if result.get("ok") else 1)
    finally:
        if not completed:
            _invalidate_match_report(report_dir, matching_input, {"status": "SHOT_MATCH_BLOCKED"})


def cmd_diagnose_timeline(args) -> int:
    from ..quality.diagnostics import diagnose_timeline
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
    recovery_input = copy.deepcopy(manifest)
    if not (manifest.get("segments") or manifest.get("videos") or
            manifest.get("material_sources")):
        return _emit({"status": "RECOVERY_MANIFEST_REQUIRED",
                      "message": "manifest 缺少 material_sources、segments 或 videos，无法安全恢复。"}, 1)
    stage = str(getattr(args, "stage", "draft_write") or "draft_write").strip().lower()
    if stage in {"semantic_match", "draft_write"}:
        from .. import ai_editor_contract
        from ..policy import playback
        try:
            playback.task_absorb_speed_cap(manifest)
            ai_editor_contract.source_reuse_limit({**manifest, "recovery_stage": stage})
        except ValueError as exc:
            if stage == "semantic_match":
                _invalidate_match_report(Path(args.report_dir).expanduser().resolve(), recovery_input,
                                         {"status": "SHOT_MATCH_BLOCKED", "message": str(exc)})
            return _emit({"status": "ENV_ERROR", "reason": str(exc),
                          "message": "速度或素材复用设置无效，未开始恢复。"}, 1)
    parent_run_id = str(manifest.get("parent_run_id") or manifest.get("run_id") or
                        manifest.get("task_id") or "").strip() or None
    manifest = dict(manifest)
    recovery_mode = str(manifest.get("delivery_mode") or "preview").strip().lower()
    if recovery_mode in {"preview_only", "预览"}:
        recovery_mode = "preview"
    manifest["delivery_mode"] = recovery_mode
    manifest["parent_run_id"] = parent_run_id
    manifest["recovery_stage"] = stage
    suffix = time.strftime("%Y%m%d_%H%M%S")
    requested_name = str(getattr(args, "draft_name", "") or "").strip()
    manifest["draft_name"] = f"{requested_name or manifest.get('draft_name') or '豆包剪映编排草稿'}_recovery_{suffix}"
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
        if stage == "semantic_match" and result.get("status") != "SHOT_MATCH_READY":
            # An early failure may occur before the matcher writes its report.
            # It must invalidate a prior successful attempt in this directory.
            backup = _invalidate_match_report(report_dir, recovery_input, result)
            if backup:
                result["previous_match_report"] = backup
        result["recovery"] = True
        result["recovery_stage"] = stage
        return logger.finalize(result)

    if recovery_mode not in {"preview", "formal"}:
        return _emit(_compact_control_result(finalize({"status": "ENV_ERROR",
            "failure_class": "environment", "reason": "INVALID_DELIVERY_MODE"})), 1)

    if stage in {"preflight", "semantic_match", "draft_write"}:
        configuration_blocker = _vision_configuration_gate(logger.runtime)
        if configuration_blocker:
            return _emit(_compact_control_result(finalize(configuration_blocker)), 1)

    if stage == "preflight":
        missing = [str(path) for path in _manifest_sources(manifest, input_path.parent)
                   if not path.is_file()]
        env = _production_environment_report(environment_report(refresh=True), manifest, require_vision=True)
        if missing or not env.get("ready"):
            result = finalize({
                "status": "ENV_ERROR",
                "failure_class": "environment",
                "message": "恢复前预检未通过，未进入生成阶段。",
                "missing": missing,
                "reason": (env.get("reason") or
                           ("INPUT_SOURCES_MISSING" if missing else "BASE_ENVIRONMENT_NOT_READY")),
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
        from ..quality.diagnostics import diagnose_timeline
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
        from .. import ai_editor_contract
        timing = None
        if manifest.get("narration_timing_file") or manifest.get("sentence_plan_version") == "semantic-v1":
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
        elif (not manifest.get("segments") and not shot_notes.has_frozen_binding(manifest)
              and (not shot_notes._notes_of(manifest) or manifest.get("shot_notes_index_basis") == "original_sentence")
              and (manifest.get("temporary_material_analysis") or manifest.get("auto_match_shots"))
              and ai_editor_contract.selection_mode(manifest) in ("ai_editor", "rule_then_ai")
              and manifest.get("semantic_sentence_planning", True)):
            result = finalize({"status": "RECOVERY_SENTENCE_PLAN_REQUIRED",
                               "failure_class": "semantic",
                               "message": "请使用原任务 sentence_planning/planned_manifest.json 恢复，避免分句错位。"})
            return _emit(_compact_control_result(result), 1)
        try:
            manifest = shot_notes.apply(manifest)
        except (ValueError, TypeError) as exc:
            return _emit(_compact_control_result(finalize({"status": "SHOT_NOTES_BINDING_INVALID",
                "failure_class": "semantic", "retryable": False, "reason": str(exc),
                "message": "画面备注无法安全绑定到原文，未开始恢复选镜。"})), 1)
        ledger = usage_ledger.ledger_path(manifest, report_dir)
        if ledger:
            manifest["usage_ledger_path"] = str(ledger)
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
        env = _production_environment_report(environment_report(refresh=True), manifest, require_vision=True)
        if not env.get("ready"):
            result = finalize({"status": "ENV_ERROR", "failure_class": "environment",
                               "reason": env.get("reason") or "BASE_ENVIRONMENT_NOT_READY",
                               "failed_stage": "preflight",
                               "message": "恢复选镜前的建稿环境未就绪；未调用视觉服务。"})
            return _emit(_compact_control_result(result), 1)
        try:
            if timing is not None:
                from . import recovery_audio
                timing = recovery_audio.prepare_for_selection(manifest, timing, report_dir, source_dir=input_path.parent)
            matched_manifest, blocker = _temporary_match(manifest, input_path, report_dir,
                                                         narration_timing=timing)
        except NarrationError as exc:
            return _emit(_compact_control_result(finalize(exc.result())), 1)
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
            "match_report": str(report_dir / "shot_match_report.json"),
        })
        return _emit(_compact_control_result(result), 0)

    if stage != "draft_write":
        result = finalize({"status": "RECOVERY_STAGE_INVALID", "message": f"不支持的恢复阶段：{stage}"})
        return _emit(_compact_control_result(result), 1)

    # draft_write 只消费已存在的匹配结果；没有缓存就明确阻断，避免恢复命令
    # 偷偷重新扫描视觉素材，造成“恢复”实际变成整稿重建。
    # Both matching entrypoints persist the successful result here; the
    # separate matched_manifest.json is a bare build input, not match evidence.
    cached_path = report_dir / "shot_match_report.json"
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
    # Never infer successful matching from bare input segments, nor fall back
    # to an older result when the current match report is invalid or blocked.
    cached = cached_result.get("manifest") if isinstance(cached_result, dict) else None
    if (not isinstance(cached_result, dict) or cached_result.get("ok") is not True
            or not isinstance(cached, dict)
            or not (cached.get("segments") or cached.get("videos"))):
        result = finalize({
            "status": "RECOVERY_MATCHED_MANIFEST_REQUIRED",
            "failure_class": "semantic",
            "message": "匹配结果缺少成功状态或已匹配的 segments/videos，未进入写稿。",
            "matched_manifest": str(cached_path),
        })
        return _emit(_compact_control_result(result), 1)
    if cached_result.get("input_manifest") != recovery_input:
        result = finalize({
            "status": "RECOVERY_MATCHED_MANIFEST_REQUIRED", "failure_class": "semantic",
            "reason": "RECOVERY_MATCH_INPUT_MISMATCH",
            "message": "匹配报告未绑定当前原始输入；请基于已有分析显式恢复 semantic_match。",
            "matched_manifest": str(cached_path),
        })
        return _emit(_compact_control_result(result), 1)
    stored_modes = []
    for value in (cached.get("delivery_mode"), cached_result.get("delivery_mode")):
        if value is not None and str(value).strip():
            mode_value = str(value).strip().lower()
            stored_modes.append("preview" if mode_value in {"preview_only", "预览"} else mode_value)
    if any(value != recovery_mode for value in stored_modes) or (not stored_modes and recovery_mode == "formal"):
        return _emit(_compact_control_result(finalize({
            "status": "RECOVERY_MATCHED_MANIFEST_REQUIRED", "failure_class": "semantic",
            "reason": "RECOVERY_DELIVERY_MODE_MISMATCH",
            "message": "匹配结果与恢复任务的交付模式不一致或缺少正式模式证据；未进入写稿。",
            "matched_manifest": str(cached_path),
        })), 1)
    recovery_controls = {key: manifest[key] for key in
                         ("draft_name", "force_overwrite", "recovery_stage", "parent_run_id", "delivery_mode")}
    manifest.update(cached)
    manifest.update(recovery_controls)
    ledger = usage_ledger.ledger_path(recovery_input, report_dir)
    if ledger:
        manifest["usage_ledger_path"] = str(ledger)
    from .. import ai_editor_contract
    active_rules = ai_editor_contract.rules_identity(manifest)
    for segment in manifest.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for part in segment.get("span_parts") or [segment]:
            if not isinstance(part, dict) or not isinstance(part.get("ai_selection"), dict):
                continue
            receipt = part["ai_selection"]
            recorded_rules = (receipt.get("identity") or {}).get("editorial_rules") or {}
            if receipt.get("schema") == ai_editor_contract.RECEIPT_SCHEMA and (
                    receipt.get("policy_version") != ai_editor_contract.POLICY_VERSION
                    or {key: recorded_rules.get(key) for key in active_rules} != active_rules):
                return _emit(_compact_control_result(finalize({"status": "AI_SELECTION_RULES_CHANGED",
                    "resume_stage": "semantic_match", "failure_class": "semantic",
                    "message": "原选镜使用旧规则，需要用冻结配音和素材观察重新匹配。"})), 1)
    from . import recovery_audio
    try:
        manifest = recovery_audio.bind_and_record(manifest, cached_result, report_dir)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _emit(_compact_control_result(finalize({"status": "RECOVERY_SENTENCE_TIMING_REQUIRED",
            "failure_class": "environment", "reason": str(exc),
            "message": "原配音或句长无法复用，未重新合成、未写入草稿。"})), 1)
    matched_manifest_sha256 = hashlib.sha256(
        json.dumps(cached_result, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    manifest["temporary_material_analysis"] = False
    manifest["auto_match_shots"] = False
    eng = OrchestrationEngine(manifest, input_path.parent, report_dir, logger=logger)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = eng.run()
    except NarrationError as exc:
        result = exc.result()
    except Exception as exc:  # noqa: BLE001
        result = {"status": "ERROR", "error": str(exc), "error_type": type(exc).__name__}
    if (result.get("status") == "NARRATION_GAP_EXCEEDED" and manifest.get("retained_tts_audio")
            and not manifest.get("_paper_fixed_narrations")):
        result.update(resume_stage="semantic_match",
                      message="旧配音超过句间气口上限。用已有素材观察和原配音恢复选镜，按处理后的句长重新排画面。")
    ledger = usage_ledger.ledger_path(manifest, report_dir)
    if ledger:
        ledger_manifest = {**manifest, "usage_ledger_path": str(ledger)}
        try:
            if result.get("status") == "SUCCESS":
                result["usage_ledger"] = usage_ledger.record(ledger_manifest, manifest.get("segments") or [])
            if cached.get("draft_name") and cached["draft_name"] != manifest.get("draft_name"):
                usage_ledger.release({**ledger_manifest, "draft_name": cached["draft_name"]})
        except (OSError, TimeoutError, ValueError) as exc:
            logger.event("usage_ledger_failed", error=str(exc))
    result["matched_manifest_sha256"] = matched_manifest_sha256
    result = finalize(result)
    return _emit(_compact_control_result(result), 0 if result.get("status") == "SUCCESS" else 1)


def cmd_migrate_encrypted(args) -> int:
    """Hand encrypted drafts to JianYing's official copy/migration flow."""
    from ..delivery.migration import migrate_encrypted
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
                and not self.m.get("voice")
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
    # The normal draft writer requires a real audio track even for muted clips.
    # A deterministic local test tone keeps calibration self-contained and does
    # not select a voice or music track for any production task.
    import math
    import struct
    import wave
    test_audio = isolated / "calibration_tone.wav"
    sample_rate = 16000
    second = b"".join(struct.pack("<h", round(4000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
                      for i in range(sample_rate))
    with wave.open(str(test_audio), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        for _ in range(max(1, args.max_sources) * 3):
            audio.writeframesraw(second)
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
            "tail_pad_s": 0,
            "bgm": {"path": str(test_audio)},
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

    env_parser = sub.add_parser("env", help="环境自检，默认包含所选建稿后端")
    env_parser.add_argument("--profile", choices=("production", "base"), default="production",
                            help="production 检查默认生产；base 仅检查通用工具链")
    env_parser.add_argument("--input", help="按任务 manifest 的写稿后端和字体配置检查")
    env_parser.set_defaults(func=cmd_env)

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
    bd.add_argument("--delivery-mode", choices=("formal", "preview"), default=None,
                    help="formal：时序无解即阻断，不得静默通过；"
                         "preview：未解出的段落按素材可用长度出占位画面并在成片上打"
                         "未认证标注，仅供预览，不得用于正式交付；省略时使用清单声明，未声明则为 preview。")
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
    rc.add_argument("--draft-name", default="", help="恢复草稿的本轮名称前缀（仍自动添加唯一后缀）")
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
        return _run_command(args.func, args)

    # 旧式：--input x [--scan-only]
    legacy = argparse.ArgumentParser(add_help=False)
    legacy.add_argument("--input")
    legacy.add_argument("--report-dir", default=".jy_run")
    legacy.add_argument("--scan-only", action="store_true")
    legacy.add_argument("--analysis")
    known, _ = legacy.parse_known_args(argv)
    if known.input:
        if known.scan_only:
            return _run_command(cmd_scan, argparse.Namespace(input=known.input,
                                                             report_dir=known.report_dir))
        return _run_command(cmd_build, argparse.Namespace(input=known.input,
                                                          report_dir=known.report_dir,
                                                          strict_script=False))
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
