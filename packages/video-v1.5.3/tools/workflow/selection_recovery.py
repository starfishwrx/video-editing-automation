"""Resume a transient selection failure from frozen audio and observations."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("SELECTION_RECOVERY_INPUT_INVALID")
    return value


def pending(report_dir: Path) -> bool:
    state = Path(report_dir) / "selection_recovery_state.json"
    if state.exists():
        try:
            record = _read(state)
            generation = _read(Path(report_dir) / "selection_resume.json").get("run_id")
            if record.get("source_run_id") in (None, generation) and record.get("status") != "SUCCESS":
                return True
        except (OSError, ValueError):
            return True  # A damaged/interrupted recovery must never become a new build.
    try:
        result = _read(Path(report_dir) / "last_result.json")
    except (OSError, ValueError):
        return False
    return result.get("retryable") is True and result.get("resume_stage") == "semantic_match"


def _logical(manifest: dict) -> dict:
    return {k: v for k, v in manifest.items() if k not in {
        "draft_name", "parent_run_id", "force_overwrite", "shot_analysis", "shot_analysis_file", "_selection_attempt",
        "qc_rejected_shots", "ai_refill_indices", "ai_refill_feedback"}}


def freeze_refill_input(manifest: dict, report_dir: Path) -> dict:
    """Reuse the exact planned sentences and verified audio of the current cut."""
    frozen = _read(Path(report_dir) / "selection_resume.json")
    if _logical(frozen["input_manifest"]) != _logical(manifest):
        raise ValueError("REFILL_ORIGINAL_INPUT_CHANGED")
    from orchestrator.runtime.file_safety import content_identity
    from orchestrator.selection import timing_contract
    prepared, timing = frozen["manifest"], frozen["timing"]
    rows, files = timing.get("rows") or [], frozen.get("audio_files") or []
    sentences = timing_contract.split_sentences(prepared)
    if len(sentences) != len(rows) or len(files) != len(rows) or not rows:
        raise ValueError("REFILL_FROZEN_AUDIO_REQUIRED")
    segments = []
    for index, (sentence, row, identity) in enumerate(zip(sentences, rows, files), 1):
        path = str(row.get("tts_path") or "")
        if (not path or row.get("timing_certainty") != "audio"
                or int(row.get("audio_duration_us") or 0) <= 0
                or str(Path(path).resolve()) != str(identity.get("path") or "")
                or content_identity(path) != identity
                or row.get("key") != timing_contract.sentence_key(sentence, index)
                or row.get("text") != timing_contract.sentence_text(sentence)):
            raise ValueError("REFILL_FROZEN_AUDIO_CHANGED")
        segment = dict(sentence) if isinstance(sentence, dict) else {"text": str(sentence)}
        segment.update(audio=path, audio_duration_us=int(row["audio_duration_us"]), timing_certainty="audio")
        segments.append(segment)
    resumed = dict(prepared)
    for key in ("draft_name", "force_overwrite", "delivery_mode", "parent_run_id",
                "recovery_stage", "run_id", "task_id", "_selection_attempt", "variant_index",
                "variant_count", "qc_rejected_shots", "ai_refill_indices", "ai_refill_feedback"):
        resumed.pop(key, None)
        if key in manifest:
            resumed[key] = manifest[key]
    resumed["segments"] = segments
    return resumed


def _invalidate_match_report(report_dir: Path, matching_input: dict, failure: dict) -> None:
    """Archive prior matching evidence before recording this failed attempt."""
    from orchestrator.runtime.file_safety import atomic_write_text
    path = report_dir / "shot_match_report.json"
    try:
        current = _read(path)
    except (OSError, ValueError):
        current = {}
    if current.get("ok") is not False or current.get("input_manifest") != matching_input:
        if path.is_file():
            raw = path.read_bytes()
            if raw:
                archive = report_dir / "match_history"
                archive.mkdir(parents=True, exist_ok=True)
                backup = archive / (hashlib.sha256(raw).hexdigest() + ".json")
                try:
                    with backup.open("xb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    if backup.read_bytes() != raw:
                        raise OSError("MATCH_REPORT_ARCHIVE_CONFLICT")
        current = {}
    current.update({"ok": False, "input_manifest": matching_input,
                    "status": failure.get("status", "SHOT_MATCH_BLOCKED"),
                    "message": failure.get("message", "本次匹配未完成，旧成功结果不可用于恢复。")})
    report_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(current, ensure_ascii=False, indent=2))


def selection_timeout(report_dir: Path, *, previous_run_id: str | None = None) -> dict | None:
    """A killed CLI cannot finalize its own log; retain only a failed stage marker."""
    report_dir = Path(report_dir)
    try:
        frozen = _read(report_dir / "selection_resume.json")
        plan = _read(report_dir / "ai_editor" / "plan.json")
        if (not frozen.get("run_id") or frozen["run_id"] == previous_run_id
                or plan.get("attempt_run_id") != frozen["run_id"]
                or plan.get("ok") is True
                or plan.get("phase") not in {"selection_inventory", "selection_candidate_review", "selection_edit"}
                or not (report_dir / "shot_candidates.json").is_file()):
            return None
    except (OSError, ValueError):
        return None
    failure = {"status": "VISION_CALL_FAILED", "failure_class": "vision",
               "failure_code": "VISION_CALL_FAILED", "reason": "SELECTION_DEADLINE_EXCEEDED",
               "message": "选镜阶段超过时间预算，已保留原配音、句长和素材观察。",
               "retryable": True, "resume_stage": "semantic_match", "failed_stage": plan["phase"]}
    (report_dir / "last_result.json").write_text(json.dumps(failure, ensure_ascii=False), encoding="utf-8")
    # Use the existing report-history mechanism before replacing a prior
    # successful match needed to audit an earlier draft.
    _invalidate_match_report(report_dir, frozen["input_manifest"], failure)
    return {"rc": 1, "stdout": json.dumps(failure, ensure_ascii=False), "stderr": "",
            "combined": json.dumps(failure, ensure_ascii=False)}


def completed(report_dir: Path, *, previous_run_id: str | None = None) -> bool:
    try:
        frozen = _read(Path(report_dir) / "selection_resume.json")
        plan = _read(Path(report_dir) / "ai_editor" / "plan.json")
        matched = _read(Path(report_dir) / "shot_match_report.json")
        return (bool(frozen.get("run_id")) and frozen["run_id"] != previous_run_id
                and plan.get("attempt_run_id") == frozen["run_id"]
                and plan.get("ok") is True and matched.get("ok") is True
                and matched.get("input_manifest") == frozen["input_manifest"])
    except (OSError, ValueError, KeyError):
        return False


def draft_pending(report_dir: Path) -> bool:
    try:
        state = _read(Path(report_dir) / "last_result.json")
        return state.get("retryable") is True and state.get("resume_stage") == "draft_write"
    except (OSError, ValueError):
        return False


def recover_draft(manifest: dict, report_dir: Path, *, prefix: list[str], run, env: dict,
                  cwd: str, deadline: float, drafts_root: Path) -> dict:
    """Use the already matched input; the CLI independently checks audio and identity."""
    report_dir = Path(report_dir)
    try:
        matched = _read(report_dir / "shot_match_report.json")
        original = matched.get("input_manifest")
        if matched.get("ok") is not True or not isinstance(original, dict):
            raise ValueError("DRAFT_RECOVERY_MATCH_INPUT_MISMATCH")
        if _logical(original) != _logical(manifest):
            # A local refill matched the fully enriched frozen input, while
            # the next worker starts from the original request. Rebuild and
            # verify every sentence and audio identity before comparing it.
            if not original.get("ai_refill_indices"):
                raise ValueError("DRAFT_RECOVERY_MATCH_INPUT_MISMATCH")
            derived = freeze_refill_input(manifest, report_dir)
            if _logical(original) != _logical(derived):
                raise ValueError("DRAFT_RECOVERY_MATCH_INPUT_MISMATCH")
        input_path = report_dir / "draft_recovery_input.json"
        input_path.write_text(json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")
        remaining = deadline - time.time()
        if remaining <= 0:
            raise ValueError("DRAFT_RECOVERY_DEADLINE_EXCEEDED")
        process = run([*prefix, "recover", "--input", str(input_path), "--report-dir", str(report_dir),
                       "--draft-name", str(manifest.get("draft_name") or ""), "--stage", "draft_write"], cwd=cwd, env=env, capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=remaining + 30)
        if process.returncode:
            return {"rc": process.returncode, "stdout": process.stdout or "", "stderr": process.stderr or "",
                    "combined": (process.stdout or "") + "\n" + (process.stderr or ""),
                    "stage_retry_exhausted": True}
        result = _read(report_dir / "last_result.json")
        draft = Path(str(result.get("draft_path") or ""))
        if result.get("status") != "SUCCESS" or draft.resolve().parent != drafts_root.resolve() or not draft.is_dir():
            raise ValueError("DRAFT_RECOVERY_DRAFT_INVALID")
        return {"rc": 0, "stdout": process.stdout or "", "stderr": process.stderr or "",
                "combined": process.stdout or "", "draft_name": draft.name, "selection_recovered": True}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        error = "DRAFT_RECOVERY_BLOCKED:" + str(exc)
        return {"rc": 1, "stdout": error, "stderr": "", "combined": error,
                "stage_retry_exhausted": True}


def archive_state(report_dir: Path) -> None:
    path = Path(report_dir) / "selection_recovery_state.json"
    if path.is_file():
        path.rename(path.with_name(f"selection_recovery_state.{time.time_ns()}.json"))


def recover(manifest: dict, report_dir: Path, *, prefix: list[str], run, env: dict,
            cwd: str, deadline: float, drafts_root: Path) -> dict:
    """One persisted stage retry; never substitute a new build on missing input."""
    report_dir = Path(report_dir)
    state_path = report_dir / "selection_recovery_state.json"
    outputs, errors = [], []
    preflight_started = False

    def result(code, reason, *, preflight_only=False, **extra):
        payload = {"status": "SELECTION_RECOVERY_BLOCKED", "failure_class": "environment",
                   "reason": reason, "retryable": bool(preflight_only), **extra}
        if preflight_only:
            payload.update(failure_code="ENVIRONMENT_PRECHECK_FAILED", failed_stage="preflight",
                           resume_stage="semantic_match")
        return {"rc": code, "stdout": json.dumps(payload, ensure_ascii=False),
                "stderr": "\n".join(errors), "combined": json.dumps(payload, ensure_ascii=False),
                "stage_retry_exhausted": not preflight_only, "preflight_only": bool(preflight_only)}

    try:
        frozen = _read(report_dir / "selection_resume.json")
        state = _read(state_path) if state_path.exists() else {}
        if state.get("source_run_id") not in (None, frozen.get("run_id")):
            state = {}
        if state.get("attempts", 0) >= 1:
            return result(1, "SELECTION_STAGE_RETRY_EXHAUSTED")
        if _logical(frozen["input_manifest"]) != _logical(manifest):
            return result(1, "SELECTION_RECOVERY_INPUT_CHANGED")
        timing = frozen["timing"]
        rows, files = timing.get("rows", []), frozen.get("audio_files", [])
        if not rows or len(files) != len(rows) or any(
                int(r.get("audio_duration_us") or 0) <= 0 or r.get("timing_certainty") != "audio" for r in rows):
            return result(1, "SELECTION_RECOVERY_AUDIO_REQUIRED")
        from orchestrator.audio import bgm_request
        bgm_path = bgm_request.normalize(frozen["manifest"].get("bgm")).get("path")
        bgm_identity = frozen.get("bgm_file")
        if bgm_path and (not isinstance(bgm_identity, dict) or
                         str(Path(str(bgm_path)).resolve()) != str(bgm_identity.get("path") or "")):
            return result(1, "SELECTION_RECOVERY_BGM_IDENTITY_REQUIRED")

        def check_audio():
            from orchestrator.runtime.file_safety import content_identity
            for row in files:
                try:
                    stat = Path(row["path"]).stat()
                except OSError as exc:
                    raise ValueError("SELECTION_RECOVERY_AUDIO_REQUIRED") from exc
                if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
                    raise ValueError("SELECTION_RECOVERY_AUDIO_CHANGED")
                if row.get("sha256"):
                    try:
                        same_audio = content_identity(row["path"]) == row
                    except (OSError, ValueError):
                        same_audio = False
                    if not same_audio:
                        raise ValueError("SELECTION_RECOVERY_AUDIO_CHANGED")
            if bgm_identity:
                try:
                    same_bgm = content_identity(bgm_identity["path"]) == bgm_identity
                except (OSError, ValueError, KeyError, TypeError):
                    same_bgm = False
                if not same_bgm:
                    raise ValueError("SELECTION_RECOVERY_BGM_CHANGED")
        check_audio()
        if not (report_dir / "shot_candidates.json").is_file():
            return result(1, "SELECTION_RECOVERY_ANALYSIS_REQUIRED")
        recovery_input = dict(frozen["manifest"])
        recovery_input["parent_run_id"] = frozen["run_id"]
        recovery_input["draft_name"] = manifest["draft_name"]
        recovery_input["narration_timing_file"] = str(report_dir / "narration_timing.json")
        # The timing snapshot must still be the exact one used for selection.
        if _read(report_dir / "narration_timing.json") != timing:
            return result(1, "SELECTION_RECOVERY_TIMING_CHANGED")
        input_path = report_dir / "selection_recovery_input.json"
        input_path.write_text(json.dumps(recovery_input, ensure_ascii=False, indent=2), encoding="utf-8")
        state = {"attempts": 0, "stage": "preflight", "status": "RUNNING", "source_run_id": frozen["run_id"]}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        for stage in ("preflight", "semantic_match", "draft_write"):
            preflight_started = False
            state.update(stage=stage)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            check_audio()
            remaining = deadline - time.time()
            if remaining <= 0:
                return result(1, "SELECTION_STAGE_DEADLINE_EXCEEDED", preflight_only=stage == "preflight")
            if stage == "semantic_match":
                # Persist consumption immediately before starting paid selection.
                state["attempts"] = 1
                state_path.write_text(json.dumps(state), encoding="utf-8")
            command = [*prefix, "recover", "--input", str(input_path), "--report-dir", str(report_dir), "--stage", stage]
            preflight_started = stage == "preflight"
            process = run(command, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=remaining + 30)
            outputs.append(process.stdout or "")
            errors.append(process.stderr or "")
            # Identity changes take precedence over a retryable preflight
            # error, including when the child itself exited unsuccessfully.
            check_audio()
            if process.returncode:
                if stage == "preflight":
                    state.update(status="PREFLIGHT_BLOCKED")
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    return result(process.returncode, "SELECTION_RECOVERY_PREFLIGHT_FAILED",
                                  preflight_only=True, error=outputs[-1][-4000:])
                if stage == "semantic_match":
                    try:
                        blocked = json.loads(process.stdout or "")
                    except (ValueError, TypeError):
                        blocked = {}
                    if (isinstance(blocked, dict) and blocked.get("status") == "ENV_ERROR"
                            and blocked.get("failure_class") == "environment"
                            and blocked.get("failed_stage") == "preflight"):
                        state.update(attempts=0, status="PREFLIGHT_BLOCKED", stage="preflight")
                        state_path.write_text(json.dumps(state), encoding="utf-8")
                        return result(process.returncode, "SELECTION_RECOVERY_PREFLIGHT_FAILED",
                                      preflight_only=True, error=outputs[-1][-4000:])
                return result(process.returncode, "SELECTION_STAGE_RETRY_EXHAUSTED", error=outputs[-1][-4000:])
        last = _read(report_dir / "last_result.json")
        draft = Path(str(last.get("draft_path") or ""))
        if last.get("status") != "SUCCESS" or draft.resolve().parent != drafts_root.resolve() or not draft.is_dir():
            return result(1, "SELECTION_RECOVERY_DRAFT_INVALID")
        state.update(status="SUCCESS")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return {"rc": 0, "stdout": outputs[-1], "stderr": "\n".join(errors),
                "combined": "\n".join(outputs + errors), "manifest": str(input_path),
                "draft_name": draft.name, "selection_recovered": True}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        verify = locals().get("check_audio")
        if callable(verify):
            try:
                verify()
            except (OSError, ValueError, TypeError, KeyError) as identity_exc:
                message = str(identity_exc)
                exc = ValueError(message if message.startswith(("SELECTION_RECOVERY_AUDIO_", "SELECTION_RECOVERY_BGM_"))
                                 else "SELECTION_RECOVERY_AUDIO_CHANGED")
        identity_error = str(exc).startswith(("SELECTION_RECOVERY_AUDIO_", "SELECTION_RECOVERY_BGM_",
                                              "FILE_CHANGED_DURING_HASH"))
        is_preflight = (not identity_error and preflight_started and isinstance(locals().get("state"), dict)
                        and state.get("attempts") == 0)
        return result(1, str(exc), preflight_only=is_preflight)
