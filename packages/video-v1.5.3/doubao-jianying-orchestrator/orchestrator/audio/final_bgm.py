"""Prepare task-selected music for both working render and native draft export."""
import hashlib
import json
import math
from pathlib import Path
import subprocess

from . import bgm_selector
from ..domain.timeline import TimelineError
from ..media.probe import probe_media
from ..runtime.file_safety import file_lock


class _UnusableTrack(TimelineError):
    """A particular candidate is unusable; tool/configuration errors stay terminal."""


def _render_source(source, out, *, duration_us, ffmpeg, ffprobe, target_lufs, fade_in, fade_out, start):
    try:
        source = Path(source).resolve(strict=True)
    except FileNotFoundError as exc:
        raise _UnusableTrack("BGM_SOURCE_MISSING") from exc
    try:
        source_facts = probe_media(source, ffprobe=ffprobe)
    except subprocess.CalledProcessError as exc:
        if not bgm_selector._tool_environment_error(exc.stderr) and bgm_selector._source_decode_error(exc.stderr):
            raise _UnusableTrack("BGM_SOURCE_UNREADABLE") from exc
        raise bgm_selector.BgmToolError("BGM_FFPROBE_PROCESS_FAILED") from exc
    except ValueError as exc:
        if str(exc) in {"MEDIA_DURATION_UNKNOWN", "MEDIA_DURATION_INVALID"}:
            raise _UnusableTrack(str(exc)) from exc
        raise
    if not source_facts["audio"] or any(s.get("codec_type") == "video" for s in source_facts["streams"]):
        raise _UnusableTrack("BGM_SOURCE_AUDIO_ONLY_REQUIRED")
    try:
        prepared = bgm_selector.prepare_bgm_track(source, out / "processed.m4a", duration_us / 1_000_000,
            ffmpeg=ffmpeg, target_lufs=target_lufs, fade_in=fade_in, fade_out=fade_out,
            start=start, source_duration=source_facts["duration_us"] / 1_000_000)
    except ValueError as exc:
        if isinstance(exc, bgm_selector.BgmSourceDecodeError):
            raise _UnusableTrack(str(exc)) from exc
        if str(exc) in {"BGM_SOURCE_WINDOW_TOO_SHORT", "BGM source has no audible music in the requested window",
                        "prepared BGM contains no audible music"}:
            raise _UnusableTrack(str(exc)) from exc
        raise
    final = out / "bgm_final.wav"
    samples = (duration_us * 48000 + 999999) // 1_000_000
    conversion = subprocess.run([str(ffmpeg), "-v", "error", "-nostdin", "-i", str(prepared), "-vn",
        "-af", f"aresample=48000,apad,atrim=end_sample={samples}", "-ar", "48000", "-ac", "2",
        "-c:a", "pcm_s16le", str(final)], capture_output=True, check=True, timeout=180)
    bgm_selector._check_ffmpeg_result(conversion)
    facts = probe_media(final, ffprobe=ffprobe)
    measured = bgm_selector._integrated_lufs(ffmpeg, final)
    if not facts["audio"] or facts["duration_us"] < duration_us or measured is None or abs(measured - target_lufs) > 1.5:
        raise TimelineError("BGM_FINAL_AUDIO_INVALID")
    return source, source_facts, final, facts, measured, samples


def prepare_task_bgm(spec, *, script, task_id, batch_id, state_dir, output_dir,
                     duration_us, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    if not isinstance(spec, dict) or not spec:
        raise TimelineError("NORMALIZED_TASK_BGM_REQUIRED")
    if set(spec) - {"path", "music_id", "mood", "auto", "script", "target_lufs", "fade_in", "fade_out", "start"}:
        raise TimelineError("TASK_BGM_OPTION_UNSUPPORTED")
    target_lufs = float(spec.get("target_lufs", -28))
    fade_in, fade_out = float(spec.get("fade_in", .4)), float(spec.get("fade_out", .2))
    if not (math.isfinite(target_lufs) and -60 <= target_lufs <= -12
            and 0 <= fade_in <= duration_us / 1_000_000 and 0 <= fade_out <= duration_us / 1_000_000):
        raise TimelineError("TASK_BGM_LEVEL_OR_FADE_INVALID")
    state = Path(state_dir)
    identity = {"task_id": task_id, "script_sha256": hashlib.sha256(script.encode()).hexdigest()}
    # Hold the task lock until the candidate has been fully processed and its
    # choice confirmed. A concurrent failed attempt cannot replace a success.
    task_key = hashlib.sha256(bgm_selector._stable_json(identity).encode("utf-8")).hexdigest()
    if not spec.get("path") and not spec.get("music_id"):
        with file_lock(state / (".prepare-" + task_key), timeout=1200):
            return _prepare(spec, script=script, task_id=task_id, batch_id=batch_id, state=state,
                identity=identity, output_dir=output_dir, duration_us=duration_us, ffmpeg=ffmpeg,
                ffprobe=ffprobe, target_lufs=target_lufs, fade_in=fade_in, fade_out=fade_out)
    return _prepare(spec, script=script, task_id=task_id, batch_id=batch_id, state=state,
        identity=identity, output_dir=output_dir, duration_us=duration_us, ffmpeg=ffmpeg,
        ffprobe=ffprobe, target_lufs=target_lufs, fade_in=fade_in, fade_out=fade_out)


def _prepare(spec, *, script, task_id, batch_id, state, identity, output_dir,
             duration_us, ffmpeg, ffprobe, target_lufs, fade_in, fade_out):
    out = Path(output_dir).resolve()
    if out.exists():
        raise TimelineError("BGM_OUTPUT_EXISTS")
    out.mkdir(parents=True, exist_ok=False)
    requested = str(spec.get("music_id") or "")
    explicit = bool(spec.get("path") or requested)
    candidates = [] if spec.get("path") else bgm_selector.select_bgm(
        spec.get("mood", ""), music_id=requested, script=spec.get("script") or script,
        topk=5, min_duration=duration_us / 1_000_000)
    if not spec.get("path") and not candidates:
        raise TimelineError("BGM_REQUIRED_NO_MATCHING_TRACK")
    failed, attempts = set(), []
    while True:
        if spec.get("path"):
            chosen = {"path": str(Path(spec["path"]).resolve(strict=True)), "selection_mode": "explicit_path"}
            source = Path(chosen["path"])
        else:
            chosen = bgm_selector.select_bgm_for_task(candidates, task_identity=identity,
                batch_identity=batch_id or task_id, state_dir=state,
                explicit_music_id=str(candidates[0]["music_id"]) if requested else "",
                excluded_candidate_hashes=failed)
            if chosen.get("available") is False:
                raise TimelineError("BGM_REQUIRED_DOWNLOAD_FAILED")
            if requested and requested != str(chosen.get("music_id") or ""):
                chosen = dict(chosen, requested_label=requested)
            attempts.append(str(chosen.get("music_id") or ""))
            source = bgm_selector.download_track(chosen, out / "source")
        try:
            if source is None:
                raise _UnusableTrack("BGM_REQUIRED_DOWNLOAD_FAILED")
            rendered = _render_source(source, out / ("candidate-" + str(len(attempts))),
                duration_us=duration_us, ffmpeg=ffmpeg, ffprobe=ffprobe, target_lufs=target_lufs,
                fade_in=fade_in, fade_out=fade_out, start=float(spec.get("start", 0)))
        except _UnusableTrack:
            if explicit or chosen.get("confirmed"):
                raise
            failed.add(bgm_selector._candidate_identity(chosen))
            continue
        break
    source, source_facts, final, facts, measured, samples = rendered
    if not explicit:
        bgm_selector.confirm_bgm_for_task(identity, state, chosen)
    receipt = {"schema": "final-task-bgm-v1", "status": "BGM_PREPARED", "task_id": task_id,
        "selected": {k: v for k, v in chosen.items() if k != "url"}, "attempted_music_ids": attempts,
        "source": str(source), "path": str(final), "source_duration_us": source_facts["duration_us"],
        "asset_duration_us": facts["duration_us"], "event_duration_us": duration_us,
        "sample_rate": 48000, "samples": samples, "target_lufs": target_lufs, "measured_lufs": measured,
        "fade_in_s": fade_in, "fade_out_s": fade_out, "fades_baked_into_asset": True}
    (out / "bgm_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt
