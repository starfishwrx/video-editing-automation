"""Resume a transient selection failure from frozen audio and observations."""
from __future__ import annotations

import json
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
        "draft_name", "parent_run_id", "force_overwrite", "shot_analysis", "shot_analysis_file"}}


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
    (report_dir / "shot_match_report.json").write_text(json.dumps({"ok": False,
        "input_manifest": frozen["input_manifest"], **failure}, ensure_ascii=False), encoding="utf-8")
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

    def result(code, reason, **extra):
        payload = {"status": "SELECTION_RECOVERY_BLOCKED", "failure_class": "environment",
                   "reason": reason, "retryable": False, **extra}
        return {"rc": code, "stdout": json.dumps(payload, ensure_ascii=False),
                "stderr": "\n".join(errors), "combined": json.dumps(payload, ensure_ascii=False),
                "stage_retry_exhausted": True}

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

        def check_audio():
            for row in files:
                try:
                    stat = Path(row["path"]).stat()
                except OSError as exc:
                    raise ValueError("SELECTION_RECOVERY_AUDIO_REQUIRED") from exc
                if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
                    raise ValueError("SELECTION_RECOVERY_AUDIO_CHANGED")
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
        state = {"attempts": 1, "stage": "preflight", "status": "RUNNING", "source_run_id": frozen["run_id"]}
        state_path.write_text(json.dumps(state), encoding="utf-8")
        for stage in ("preflight", "semantic_match", "draft_write"):
            state.update(stage=stage)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            check_audio()
            remaining = deadline - time.time()
            if remaining <= 0:
                return result(1, "SELECTION_STAGE_RETRY_EXHAUSTED:DEADLINE_EXCEEDED")
            command = [*prefix, "recover", "--input", str(input_path), "--report-dir", str(report_dir), "--stage", stage]
            process = run(command, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=remaining + 30)
            outputs.append(process.stdout or "")
            errors.append(process.stderr or "")
            if process.returncode:
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
        return result(1, str(exc))
