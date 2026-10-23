"""Freeze inputs and run an isolated A/B arm through the normal build CLI.

No machine paths or credentials are stored in this module. Preparing an arm
does not execute models, synthesize speech, register a production draft, or
publish anything. Every arm receives byte-identical frozen assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: str | Path, value) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _path_key(value: str | Path) -> str:
    # UNC identity does not need repeated network-backed junction resolution.
    if str(value).startswith("\\\\"):
        return os.path.normcase(os.path.normpath(str(value)))
    return os.path.normcase(str(Path(value).resolve()))


def _remap(value, mapping: dict[str, str]):
    if isinstance(value, dict):
        return {k: _remap(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, mapping) for v in value]
    if isinstance(value, str) and value:
        try:
            if not Path(value).is_absolute():
                return value
            return mapping.get(_path_key(value), value)
        except (OSError, ValueError):
            return value
    return value


def _frame_paths(row: dict):
    values = [row.get("frame_path"), *(row.get("frame_paths") or [])]
    for item in row.get("vision_frames") or []:
        if isinstance(item, dict):
            values.extend(item.get(k) for k in ("path", "frame_path", "file"))
    return [str(v) for v in values if v]


def freeze_inputs(manifest_path: Path, observations_path: Path, output: Path,
                  *, audio_segments: Path | None = None, bgm_path: Path | None = None) -> dict:
    """Make a new immutable input snapshot; fail before copying if assets are absent."""
    if output.exists():
        raise FileExistsError(f"Refusing an existing freeze directory: {output}")
    manifest = read_json(manifest_path)
    observations = read_json(observations_path)
    segments = read_json(audio_segments) if audio_segments else manifest.get("segments", [])
    if not segments or not all(s.get("audio") and int(s.get("audio_duration_us", 0)) > 0
                               for s in segments):
        raise ValueError("All narration segments require a real audio file and duration_us")
    declared = [str(p) for p in manifest.get("material_sources", [])]
    observed = [str(s["video"]) for s in observations.get("shots", []) if s.get("video")]
    paths: dict[str, dict] = {}
    missing_frames = []
    for role, group in (("material", declared + observed),
                        ("audio", [s["audio"] for s in segments])):
        for value in group:
            p = Path(value)
            if not p.is_file():
                raise FileNotFoundError(f"Missing {role}: {p}")
            paths.setdefault(_path_key(p), {"path": p, "role": role})
    if bgm_path:
        if not bgm_path.is_file():
            raise FileNotFoundError(bgm_path)
        paths.setdefault(_path_key(bgm_path), {"path": bgm_path, "role": "bgm"})
    for row in observations.get("shots", []):
        for value in _frame_paths(row):
            p = Path(value)
            if p.is_file():
                paths.setdefault(_path_key(p), {"path": p, "role": "frame"})
            else:
                missing_frames.append(str(p))
    output.mkdir(parents=True)
    mapping, assets, content_dest = {}, [], {}
    for key, item in paths.items():
        source, role = item["path"], item["role"]
        stat = source.stat()
        h = digest_file(source)
        destination = content_dest.get(h)
        if destination is None:
            destination = output / "assets" / role / (h + source.suffix.lower())
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if digest_file(destination) != h:
                raise RuntimeError(f"Frozen copy differs: {source}")
            content_dest[h] = destination
        after = source.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"Source changed while freezing: {source}")
        mapping[key] = str(destination)
        assets.append({"original_path": str(source), "path": str(destination),
                       "role": role, "sha256": h, "bytes": stat.st_size})
    frozen_observations = _remap(observations, mapping)
    frozen_observations["snapshot_provenance"] = {
        "source": str(observations_path), "sha256": digest_file(observations_path),
        "classification": "historical_model_observation", "human_certified": False}
    frozen = _remap(manifest, mapping)
    # Never inherit an old manually curated plan, prior run, delivery path, or arm.
    for key in ("contextual_plan", "parent_run_id", "draft_name", "experiment_arm",
                "usage_ledger_path", "material_index_file", "shot_analysis_file"):
        frozen.pop(key, None)
    frozen["segments"] = _remap(segments, mapping)
    for segment in frozen["segments"]:
        segment["audio_mode"] = "file"
        segment["audio_sha256"] = digest_file(segment["audio"])
        segment["timing_certainty"] = "audio"
    frozen.update(audio_selection_confirmed=True, force_overwrite=False,
                  shot_analysis_file=str(output / "observations.json"),
                  reference_style_evaluation="DEFERRED_BY_USER")
    frozen["bgm"] = {"path": mapping[_path_key(bgm_path)]} if bgm_path else None
    frozen["frozen_audio"] = {"all_segments_file": True,
                              "original_voice": manifest.get("voice"),
                              "resynthesis_allowed": False}
    write_json(output / "observations.json", frozen_observations)
    write_json(output / "manifest.json", frozen)
    receipt = {"schema": "v154-frozen-inputs-v1", "status": "FROZEN",
               "human_accepted": False, "source_manifest": str(manifest_path),
               "source_manifest_sha256": digest_file(manifest_path),
               "manifest_sha256": digest_file(output / "manifest.json"),
               "observations_sha256": digest_file(output / "observations.json"),
               "assets": assets, "missing_historical_frames": sorted(set(missing_frames)),
               "material_count": len(frozen.get("material_sources", [])),
               "shot_count": len(frozen_observations.get("shots", [])),
               "audio_count": len(frozen["segments"]),
               "audio_duration_us": sum(int(s["audio_duration_us"]) for s in frozen["segments"]),
               "historical_contextual_plan_imported": False}
    receipt["input_identity_sha256"] = canonical_hash({k: receipt[k] for k in
        ("manifest_sha256", "observations_sha256", "assets")})
    write_json(output / "freeze_receipt.json", receipt)
    return receipt


def verify_frozen(root: Path) -> dict:
    receipt = read_json(root / "freeze_receipt.json")
    for name, key in (("manifest.json", "manifest_sha256"),
                      ("observations.json", "observations_sha256")):
        if digest_file(root / name) != receipt[key]:
            raise ValueError(f"Frozen {name} was changed")
    for item in receipt["assets"]:
        if digest_file(item["path"]) != item["sha256"]:
            raise ValueError(f"Frozen asset changed: {item['path']}")
    return receipt


def prepare_arm(frozen_root: Path, package_root: Path, output: Path, strategy: str,
                *, index_root: Path | None = None, model_cache_root: Path | None = None) -> dict:
    if strategy not in ("legacy", "indexed_contextual"):
        raise ValueError("Unsupported strategy")
    if output.exists():
        raise FileExistsError(output)
    receipt = verify_frozen(frozen_root)
    if not (package_root / "doubao-jianying-orchestrator" / "run.py").is_file():
        raise FileNotFoundError("Package normal build entry is absent")
    manifest = read_json(frozen_root / "manifest.json")
    output.mkdir(parents=True)
    for folder in ("drafts", "cache", "report", "temp", "home", "gpu"):
        (output / folder).mkdir()
    manifest.update(pipeline_strategy=strategy, variant_index=1, variant_count=1,
                    draft_name=output.name, usage_ledger_path=str(output / "usage.json"),
                    delivery_mode="formal", force_overwrite=False,
                    pilot_input_identity_sha256=receipt["input_identity_sha256"])
    if index_root:
        manifest["material_index_dir"] = str(index_root)
    if model_cache_root:
        manifest["vision_model_cache_dir"] = str(model_cache_root)
    # Legacy uses exactly the frozen observations, indexed mode imports them but
    # still has to satisfy newly requested evidence through its normal path.
    manifest["vision_analysis"] = strategy == "indexed_contextual"
    write_json(output / "manifest.json", manifest)
    env = {"JY_SKILL_ROOT": str(package_root / "jianying-editor"),
           "JY_DRAFT_ROOT": str(output / "drafts"),
           "JY_DRAFTS_ROOT": str(output / "drafts"),
           "JY_PROJECTS_ROOT": str(output / "drafts"),
           "JY_VISION_CACHE_DIR": str(output / "cache"),
           "JY_GPU_CALL_LOG": str(output / "gpu" / "calls.jsonl"),
           "JY_SLOT_DIR": str((index_root.parent if index_root else frozen_root.parent) / "shared_slots"),
           "JY_VISION_SLOTS": "2",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONDONTWRITEBYTECODE": "1", "TEMP": str(output / "temp"),
           "TMP": str(output / "temp")}
    if index_root:
        env["JY_MATERIAL_INDEX_DIR"] = str(index_root)
    if model_cache_root:
        env["JY_VISION_MODEL_DIR"] = str(model_cache_root)
    command = [str(package_root / "doubao-jianying-orchestrator" / "run.py"),
               "build", "--input", str(output / "manifest.json"),
               "--delivery-mode", "formal", "--report-dir", str(output / "report")]
    data = {"schema": "v154-pilot-arm-v1", "status": "PREPARED_NOT_RUN",
            "strategy": strategy, "package_root": str(package_root),
            "package_version": (package_root / "VERSION").read_text().strip(),
            "frozen_root": str(frozen_root),
            "input_identity_sha256": receipt["input_identity_sha256"],
            "manifest_sha256": digest_file(output / "manifest.json"),
            "command_without_python": command, "environment": env,
            "publication_authorized": False}
    write_json(output / "arm_receipt.json", data)
    write_json(output / "env.json", env)
    return data


def run_arm(root: Path, python: Path, *, timeout: int = 1800) -> dict:
    """Execute only when explicitly requested; all output paths remain isolated."""
    arm = read_json(root / "arm_receipt.json")
    if (root / "execution_receipt.json").exists():
        raise FileExistsError("This arm has already run; prepare a new output directory")
    verify_frozen(Path(arm["frozen_root"]))
    if digest_file(root / "manifest.json") != arm["manifest_sha256"]:
        raise ValueError("Prepared arm manifest changed")
    env = os.environ.copy()
    env_file = root / "env.json"
    isolated_env = read_json(env_file) if env_file.is_file() else arm["environment"]
    if isolated_env != arm["environment"]:
        raise ValueError("Arm environment differs from the prepared receipt")
    env.update(isolated_env)
    # Prevent inherited production write/publication destinations.
    for key in ("JY_NAS_SHARE", "JY_NAS_USER", "NAS_PASSWORD", "LARK_BASE_TOKEN",
                "JY_MAC_DRAFT_ROOT", "JY_MATERIAL_INDEX_FILE", "JY_DRAFT_RESERVATION"):
        env.pop(key, None)
    command = [str(python), *arm["command_without_python"]]
    started = time.monotonic()
    with (root / "build.log").open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(command, cwd=root, env=env, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout, check=False)
            code, error = proc.returncode, ""
        except subprocess.TimeoutExpired:
            code, error = None, "PILOT_BUILD_TIMEOUT"
    data = {"schema": "v154-pilot-execution-v1", "command": command,
            "returncode": code, "error": error, "elapsed_s": time.monotonic() - started,
            "input_identity_sha256": arm["input_identity_sha256"],
            "manifest_sha256": arm["manifest_sha256"],
            "human_accepted": False, "production_deployed": False}
    write_json(root / "execution_receipt.json", data)
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--input", type=Path, required=True)
    freeze.add_argument("--observations", type=Path, required=True)
    freeze.add_argument("--audio-segments", type=Path)
    freeze.add_argument("--bgm", type=Path)
    freeze.add_argument("--output", type=Path, required=True)
    arm = commands.add_parser("prepare-arm")
    arm.add_argument("--frozen-root", type=Path, required=True)
    arm.add_argument("--package-root", type=Path, required=True)
    arm.add_argument("--strategy", choices=("legacy", "indexed_contextual"), required=True)
    arm.add_argument("--output", type=Path, required=True)
    arm.add_argument("--index-root", type=Path)
    arm.add_argument("--model-cache-root", type=Path)
    run = commands.add_parser("run-arm")
    run.add_argument("--arm-root", type=Path, required=True)
    run.add_argument("--python", type=Path, default=Path(sys.executable))
    run.add_argument("--timeout", type=int, default=1800)
    verify = commands.add_parser("verify")
    verify.add_argument("--frozen-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "freeze":
        result = freeze_inputs(args.input, args.observations, args.output,
                               audio_segments=args.audio_segments, bgm_path=args.bgm)
    elif args.action == "prepare-arm":
        result = prepare_arm(args.frozen_root, args.package_root, args.output, args.strategy,
                             index_root=args.index_root, model_cache_root=args.model_cache_root)
    elif args.action == "run-arm":
        result = run_arm(args.arm_root, args.python, timeout=args.timeout)
    else:
        result = verify_frozen(args.frozen_root)
    print(json.dumps({k: v for k, v in result.items() if k != "assets"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
