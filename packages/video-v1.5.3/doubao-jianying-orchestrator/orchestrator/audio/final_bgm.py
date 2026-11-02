"""Prepare task-selected music for both working render and native draft export."""
import hashlib
import json
import math
from pathlib import Path
import subprocess

from . import bgm_selector
from ..domain.timeline import TimelineError
from ..media.probe import probe_media


def prepare_task_bgm(spec, *, script, task_id, batch_id, state_dir, output_dir,
                     duration_us, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    if not isinstance(spec, dict) or not spec:
        raise TimelineError("NORMALIZED_TASK_BGM_REQUIRED")
    if set(spec) - {"path", "music_id", "mood", "auto", "script", "target_lufs", "fade_in", "fade_out", "start"}:
        raise TimelineError("TASK_BGM_OPTION_UNSUPPORTED")
    out = Path(output_dir).resolve()
    if out.exists():
        raise TimelineError("BGM_OUTPUT_EXISTS")
    out.mkdir(parents=True, exist_ok=False)
    if spec.get("path"):
        source = Path(spec["path"]).resolve(strict=True)
        chosen = {"path": str(source), "selection_mode": "explicit_path"}
    else:
        candidates = bgm_selector.select_bgm(spec.get("mood", ""), music_id=spec.get("music_id", ""),
            script=spec.get("script") or script, topk=5, min_duration=duration_us / 1_000_000)
        if not candidates:
            raise TimelineError("BGM_REQUIRED_NO_MATCHING_TRACK")
        requested = str(spec.get("music_id") or "")
        chosen = bgm_selector.select_bgm_for_task(candidates,
            task_identity={"task_id": task_id, "script_sha256": hashlib.sha256(script.encode()).hexdigest()},
            batch_identity=batch_id or task_id, state_dir=Path(state_dir),
            explicit_music_id=str(candidates[0]["music_id"]) if requested else "")
        source = bgm_selector.download_track(chosen, out / "source")
        if source is None:
            raise TimelineError("BGM_REQUIRED_DOWNLOAD_FAILED")
        source = Path(source).resolve(strict=True)
    target_lufs = float(spec.get("target_lufs", -28))
    fade_in, fade_out = float(spec.get("fade_in", .4)), float(spec.get("fade_out", .2))
    if not (math.isfinite(target_lufs) and -60 <= target_lufs <= -12
            and 0 <= fade_in <= duration_us / 1_000_000 and 0 <= fade_out <= duration_us / 1_000_000):
        raise TimelineError("TASK_BGM_LEVEL_OR_FADE_INVALID")
    source_facts = probe_media(source, ffprobe=ffprobe)
    if (not source_facts["audio"] or any(s.get("codec_type") == "video"
            for s in source_facts["streams"])):
        raise TimelineError("BGM_SOURCE_AUDIO_ONLY_REQUIRED")
    prepared = bgm_selector.prepare_bgm_track(source, out / "processed.m4a", duration_us / 1_000_000,
        ffmpeg=ffmpeg, target_lufs=target_lufs, fade_in=fade_in, fade_out=fade_out,
        start=float(spec.get("start", 0)), source_duration=source_facts["duration_us"] / 1_000_000)
    # Ceiling physical PCM samples covers the exact business endpoint. Fades
    # are baked once; the editable native event keeps its original µs length.
    final = out / "bgm_final.wav"
    samples = (duration_us * 48000 + 999999) // 1_000_000
    subprocess.run([str(ffmpeg), "-v", "error", "-nostdin", "-i", str(prepared), "-vn",
        "-af", f"aresample=48000,apad,atrim=end_sample={samples}", "-ar", "48000", "-ac", "2",
        "-c:a", "pcm_s16le", str(final)], capture_output=True, check=True, timeout=180)
    facts = probe_media(final, ffprobe=ffprobe)
    measured = bgm_selector._integrated_lufs(ffmpeg, final)
    if not facts["audio"] or facts["duration_us"] < duration_us or measured is None or abs(measured - target_lufs) > 1.5:
        raise TimelineError("BGM_FINAL_AUDIO_INVALID")
    receipt = {"schema": "final-task-bgm-v1", "status": "BGM_PREPARED", "task_id": task_id,
        "selected": {k: v for k, v in chosen.items() if k != "url"}, "source": str(source),
        "path": str(final), "source_duration_us": source_facts["duration_us"],
        "asset_duration_us": facts["duration_us"], "event_duration_us": duration_us,
        "sample_rate": 48000, "samples": samples, "target_lufs": target_lufs, "measured_lufs": measured,
        "fade_in_s": fade_in, "fade_out_s": fade_out, "fades_baked_into_asset": True}
    (out / "bgm_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt
