#!/usr/bin/env python3
"""Run the indexed test strategy with isolated paths and a copied manifest.

The launcher never edits the input, installed skills, registry or global
environment.  Model/credentials are supplied to the normal packaged run.py.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PATH_KEYS = {
    "video", "audio", "bgm", "bgm_path", "source_path", "material_dir", "material_root",
    "shot_analysis_file", "material_index_file", "product_identity_file", "reference_video",
    "voice_file", "frame_path", "evidence_file", "plan_file", "observer_file",
}
PATH_LISTS = {"material_sources", "videos", "audios", "frame_paths"}


def _overlap(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left == right or left in right.parents or right in left.parents


def _absolute(value: str, base: Path) -> str:
    if not value.strip() or "://" in value:
        return value
    path = Path(value).expanduser()
    return str((path if path.is_absolute() else base / path).resolve())


def _absolute_inputs(value, base: Path, key: str = ""):
    if isinstance(value, dict):
        return {name: _absolute_inputs(item, base, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_absolute(item, base) if key in PATH_LISTS and isinstance(item, str)
                else _absolute_inputs(item, base) for item in value]
    if key in PATH_KEYS and isinstance(value, str):
        return _absolute(value, base)
    return value


def production_draft_roots(environment: dict[str, str]) -> list[Path]:
    """Read known active/default locations; never initialize or modify them."""
    home = Path.home()
    local = Path(environment.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
    candidates = [local / app / "User Data" / "Projects" / "com.lveditor.draft"
                  for app in ("JianyingPro", "CapCut")]
    candidates.extend([home / "Movies" / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft",
                       home / "Movies" / "JianyingPro Drafts"])
    candidates.extend(Path(environment[name]).expanduser() for name in
                      ("JY_DRAFTS_ROOT", "JY_PROJECTS_ROOT", "JY_DRAFT_ROOT") if environment.get(name))
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Bytedance\JianyingPro\GlobalSettings\History") as key:
                value, _ = winreg.QueryValueEx(key, "currentCustomDraftPath")
                if value:
                    candidates.append(Path(value).expanduser())
        except OSError:
            pass
    return [path.resolve() for path in candidates]


def prepare(args, *, environment: dict[str, str] | None = None) -> dict:
    inherited = dict(os.environ if environment is None else environment)
    input_file = Path(args.input).expanduser().resolve()
    if not input_file.is_file():
        raise ValueError("input manifest does not exist")
    directories = {name: Path(getattr(args, name)).expanduser().resolve()
                   for name in ("report_dir", "index_dir", "model_cache_dir", "drafts_root")}
    for name, path in directories.items():
        if str(path).startswith("\\\\"):
            raise ValueError(f"{name} must be a local directory, not a network share")
        if _overlap(path, PACKAGE_ROOT):
            raise ValueError(f"{name} must be outside the package source tree")
        if path.exists() and not path.is_dir():
            raise ValueError(f"{name} is not a directory")
        if any(_overlap(path, root) for root in production_draft_roots(inherited)):
            raise ValueError(f"{name} overlaps a configured or default production draft root")
    items = list(directories.items())
    for index, (name, path) in enumerate(items):
        for other_name, other in items[index + 1:]:
            if _overlap(path, other):
                raise ValueError(f"{name} and {other_name} must be separate, non-nested directories")
    original = json.loads(input_file.read_text(encoding="utf-8-sig"))
    if not isinstance(original, dict):
        raise ValueError("input manifest must be a JSON object")
    manifest = _absolute_inputs(copy.deepcopy(original), input_file.parent)
    manifest.update(pipeline_strategy="indexed_contextual", delivery_mode="formal",
                    material_index_dir=str(directories["index_dir"]),
                    vision_model_cache_dir=str(directories["model_cache_dir"]),
                    usage_ledger_path=str(directories["report_dir"] / "local_shot_usage.json"))
    # These are products of this run. A copied old B receipt must not opt the new
    # task into a stale plan or stop the normal narrator/index/planner entrypoint.
    for key in ("contextual_plan", "experiment_arm", "indexed_groups"):
        manifest.pop(key, None)
    segments = manifest.get("segments") or []
    frozen = bool(segments) and all(isinstance(row, dict) and row.get("audio")
        and Path(str(row["audio"])).is_file() and type(row.get("audio_duration_us")) is int
        and row["audio_duration_us"] > 0 for row in segments)
    if frozen:
        for row in segments:
            row["audio_mode"] = "file"
        manifest["narration_source"] = "frozen_files"
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    manifest_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    copied_input = directories["report_dir"] / f"input_{manifest_sha[:16]}.json"
    child_environment = inherited.copy()
    for name in ("JY_PIPE_ROOT", "JY_WORK_ROOT", "JY_SKILL_ROOT", "JY_DRAFT_ROOT", "JY_DRAFTS_ROOT",
                 "JY_PROJECTS_ROOT", "JY_MATERIAL_INDEX_FILE", "JY_MATERIAL_INDEX_DIR", "JY_VISION_MODEL_DIR",
                 "JY_VISION_CACHE_DIR", "JY_EVAL_OBSERVATIONS", "JY_NAS_USER", "NAS_PASSWORD",
                 "LARK_BASE_TOKEN", "LARK_TABLE_ID", "PYTHONPATH"):
        child_environment.pop(name, None)
    work = directories["report_dir"] / "work"
    child_environment.update({"JY_PIPE_ROOT": str(work), "JY_WORK_ROOT": str(work),
        "JY_SKILL_ROOT": str(PACKAGE_ROOT / "jianying-editor"),
        "JY_DRAFTS_ROOT": str(directories["drafts_root"]), "JY_PROJECTS_ROOT": str(directories["drafts_root"]),
        "JY_DRAFT_ROOT": str(directories["drafts_root"]), "JY_MATERIAL_INDEX_DIR": str(directories["index_dir"]),
        "JY_VISION_MODEL_DIR": str(directories["model_cache_dir"]),
        "JY_VISION_CACHE_DIR": str(directories["index_dir"] / "legacy_vision"),
        "HF_HOME": str(directories["model_cache_dir"] / "huggingface"), "JY_EVAL_MODE": "observe",
        "PYTHONUTF8": "1"})
    command = [sys.executable, "-X", "utf8", str(PACKAGE_ROOT / "doubao-jianying-orchestrator" / "run.py"),
               "build", "--input", str(copied_input), "--report-dir", str(directories["report_dir"] / "build"),
               "--delivery-mode", "formal"]
    return {"manifest": manifest, "manifest_text": payload, "manifest_sha256": manifest_sha,
            "input": input_file, "copied_input": copied_input, "directories": directories,
            "environment": child_environment, "command": command, "frozen_audio": frozen}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Isolated indexed-contextual local test launcher")
    for flag in ("input", "report-dir", "index-dir", "model-cache-dir", "drafts-root"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--dry-run", action="store_true", help="validate and print paths without writing or calling models")
    args = parser.parse_args(argv)
    try:
        spec = prepare(args)
        receipt = {"schema": "isolated_video_test_launch.v1",
                   "package_version": (PACKAGE_ROOT / "VERSION").read_text(encoding="utf-8").strip(),
                   "input_manifest_sha256": hashlib.sha256(spec["input"].read_bytes()).hexdigest(),
                   "copied_manifest_sha256": spec["manifest_sha256"],
                   "copied_manifest": str(spec["copied_input"]),
                   "directories": {key: str(value) for key, value in spec["directories"].items()},
                   "pipeline_strategy": "indexed_contextual", "frozen_audio": spec["frozen_audio"],
                   "command": spec["command"], "formal_delivery_allowed": False,
                   "human_review": "PENDING", "production_deployed": False}
        if args.dry_run:
            print(json.dumps({**receipt, "dry_run": True}, ensure_ascii=False, indent=2))
            return 0
        for path in [*spec["directories"].values(), spec["directories"]["report_dir"] / "work"]:
            path.mkdir(parents=True, exist_ok=True)
        if (spec["copied_input"].is_file()
                and spec["copied_input"].read_text(encoding="utf-8") != spec["manifest_text"]):
            raise ValueError("existing copied manifest differs; select a fresh report directory")
        spec["copied_input"].write_text(spec["manifest_text"], encoding="utf-8")
        receipt_file = spec["directories"]["report_dir"] / "test_launcher_receipt.json"
        receipt_file.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        run = subprocess.run(spec["command"], cwd=PACKAGE_ROOT, env=spec["environment"], check=False)
        receipt["process_exit_code"] = run.returncode
        receipt_file.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        return run.returncode
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "TEST_LAUNCH_BLOCKED", "message": str(exc),
                          "formal_delivery_allowed": False}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
