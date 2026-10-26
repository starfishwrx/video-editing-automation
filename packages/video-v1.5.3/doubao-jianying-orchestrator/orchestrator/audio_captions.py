"""ASR-timed subtitles from the final, narration-only audio timeline.

The ASR adapter is deliberately injectable: unit tests can supply timestamped
words without pretending that a mock is a real recognition result.

Hugging Face downloads honor the externally configured ``HF_HUB_DOWNLOAD_TIMEOUT``.
Configure it in the launch environment; this module never changes process-wide env.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from functools import lru_cache
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from .subtitle_units import KEEP_WORDS, text_units

ASR_VERSION = "faster-whisper-adapter-v2"
HF_HUB_DOWNLOAD_TIMEOUT_DEFAULT_S = "10"
ASR_TIMEOUT_DEFAULT_S = 180.0
CONTENT_SIMILARITY_MIN = 0.95


class ASRTimeoutError(TimeoutError):
    """Recognition exceeded its configured finite wall-clock bound."""


def _get(value: Any, key: str, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _clean(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]+", "", str(value or "")).lower()


def _reference_text(manifest: dict, clips: list) -> str:
    original = str(manifest.get("full_script") or manifest.get("script") or "")
    if not original:
        seen = set()
        chunks = []
        for clip in clips:
            idx = _get(clip, "parent_index", -1)
            try:
                key = int(idx) if idx is not None else -1
            except (TypeError, ValueError):
                key = -1
            if key < 0 or key not in seen:
                seen.add(key)
                value = str(_get(clip, "text", "") or "").strip()
                if value:
                    chunks.append(value)
        original = "".join(chunks)
    return original


def _asr_hotwords(reference: str, manifest: dict) -> list[str]:
    """Hints may only come from protected terms in the script or explicit brand list."""
    normalized = _clean(reference)
    declared = manifest.get("brand_names") or manifest.get("subtitle_brands") or []
    if isinstance(declared, str):
        declared = [declared]
    terms = {word for word in KEEP_WORDS if _clean(word) and _clean(word) in normalized}
    terms.update(str(word).strip() for word in declared if str(word).strip())
    product = manifest.get("product")
    if isinstance(product, str) and product.strip() and _clean(product) in normalized:
        terms.add(product.strip())
    return sorted(terms, key=lambda word: (-len(word), word.casefold()))


_NUM_TOKEN = re.compile(r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿点]+")
_CN_DIGITS = {"零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3",
              "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def _chinese_number(value: str) -> str:
    if "点" in value:
        integer, fraction = value.split("点", 1)
        left = _chinese_integer(integer) if integer else "0"
        right = "".join(_CN_DIGITS.get(char, "") for char in fraction)
        return f"{left}.{right}".rstrip(".")
    return _chinese_integer(value)


def _chinese_integer(value: str) -> str:
    if not any(char in "十百千万亿" for char in value):
        return "".join(_CN_DIGITS.get(char, "") for char in value)
    total = section = number = 0
    small = {"十": 10, "百": 100, "千": 1000}
    large = {"万": 10_000, "亿": 100_000_000}
    for char in value:
        if char in _CN_DIGITS:
            number = int(_CN_DIGITS[char])
        elif char in small:
            section += (number or 1) * small[char]
            number = 0
        elif char in large:
            section += number
            total += (section or 1) * large[char]
            section = number = 0
    return str(total + section + number)


def _canonical_number(token: str) -> str:
    if token and token[0] in _CN_DIGITS or any(char in "十百千万亿点" for char in token):
        return _chinese_number(token)
    from decimal import Decimal, InvalidOperation
    try:
        number = Decimal(token).normalize()
        return format(number, "f")
    except InvalidOperation:
        return token


def _comparison_text(value: str) -> str:
    return _NUM_TOKEN.sub(lambda match: "N" + _canonical_number(match.group(0)) + "N", str(value or "")).lower()


def _resolve_audio(clip: Any, audio_overrides: dict | None) -> str:
    source = str(_get(clip, "tts_path") or _get(clip, "audio") or "")
    if audio_overrides:
        source_keys = [source]
        try:
            source_keys.append(str(Path(source)))
            source_keys.append(str(Path(source).resolve()))
        except (OSError, ValueError):
            pass
        for source_key in source_keys:
            if source_key in audio_overrides:
                item = audio_overrides[source_key]
                return str(item.get("path") or item.get("audio") or item) if isinstance(item, dict) else str(item)
    raw_idx = _get(clip, "audio_clip_index", _get(clip, "parent_index", -1))
    idx = int(raw_idx) if raw_idx is not None else -1
    candidates = [idx, str(idx), _get(clip, "parent_index"), str(_get(clip, "parent_index"))]
    for key in candidates:
        if audio_overrides and key in audio_overrides:
            item = audio_overrides[key]
            return str(item.get("path") or item.get("audio") or item) if isinstance(item, dict) else str(item)
    return source


def _timeline_audio(clips: list, report_dir: Path, ffmpeg: str,
                    audio_overrides: dict | None) -> tuple[Path, dict]:
    usable = []
    inputs = []
    for clip in clips:
        start = int(_get(clip, "start_us", 0) or 0)
        duration = int(_get(clip, "duration_us", 0) or 0)
        if duration <= 0:
            duration = round(float(_get(clip, "duration", 0) or 0) * 1_000_000)
        mode = str(_get(clip, "audio_mode", "tts") or "tts").lower()
        if start < 0 or duration <= 0:
            raise ValueError("clip has invalid start_us/duration")
        # The narration track is reconstructed only from the selected TTS/file asset.
        if mode not in {"tts", "file"}:
            continue
        path = _resolve_audio(clip, audio_overrides)
        if not path or not Path(path).is_file():
            raise ValueError(f"missing final narration audio for clip parent={_get(clip, 'parent_index', -1)}")
        audio_start = int(_get(clip, "audio_clip_start_us", start) or 0)
        offset = max(0, start - audio_start) if bool(_get(clip, "multi_shot", False)) else 0
        usable.append((len(inputs), start, duration, offset, path))
        inputs.append(path)
    if not usable:
        raise ValueError("no narration audio clips")

    end_us = max(s + d for _, s, d, _, _ in usable)
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "caption_narration.wav"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for path in inputs:
        cmd += ["-i", path]
    filters = []
    labels = []
    for input_idx, start, duration, offset, _ in usable:
        label = f"a{input_idx}"
        delay_ms = round(start / 1000)
        filters.append(f"[{input_idx}:a]atrim=start={offset/1e6:.6f}:duration={duration/1e6:.6f},asetpts=PTS-STARTPTS,adelay={delay_ms}|{delay_ms}[{label}]")
        labels.append(f"[{label}]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,atrim=duration={end_us/1e6:.6f}[out]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode or not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError(f"failed to assemble narration-only timeline: {(proc.stderr or '')[-500:]}")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return output, {"sha256": digest, "duration_us": end_us, "inputs": [p for p in inputs], "clips": len(usable)}


def transcribe_faster_whisper(audio_path: Path, *, model_name: str | None = None,
                              language: str | None = None,
                              hotwords: list[str] | None = None) -> dict:
    """Real optional faster-whisper adapter; no silent recognition fallback."""
    model_name = model_name or os.environ.get("JY_ASR_MODEL", "small")
    device, compute = _preferred_backend()
    try:
        return _transcribe_with_backend(audio_path, model_name, device, compute, language, hotwords)
    except Exception as exc:
        if device != "cuda" or not _is_cuda_runtime_error(exc):
            raise RuntimeError(f"faster-whisper transcription failed ({device}/{compute}): {exc}") from exc
        fallback = f"CUDA transcription failed ({type(exc).__name__}: {exc}); retried on CPU int8"
        _load_whisper_model.cache_clear()
        import gc
        gc.collect()
        result = _transcribe_with_backend(audio_path, model_name, "cpu", "int8", language, hotwords)
        result["fallback"] = fallback
        return result


def _transcribe_in_child(audio_path: Path, *, model_name: str, language: str | None,
                         hotwords: list[str] | None, timeout_s: float) -> dict:
    """Run model loading/download/decode in a killable, bounded worker process."""
    package_parent = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (str(package_parent) + os.pathsep + current_pythonpath
                         if current_pythonpath else str(package_parent))
    payload = {"audio_path": str(audio_path), "model_name": model_name,
               "language": language, "hotwords": hotwords}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "orchestrator.audio_captions", "transcribe"],
            input=json.dumps(payload, ensure_ascii=False), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout_s, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ASRTimeoutError(f"ASR_TIMEOUT after {timeout_s:g}s") from exc
    if proc.returncode:
        raise RuntimeError(f"ASR worker failed ({proc.returncode}): {(proc.stderr or '')[-800:]}")
    try:
        result = json.loads(proc.stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"ASR worker returned invalid JSON: {(proc.stdout or '')[-400:]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("ASR worker response must be a JSON object")
    return result


def _is_cuda_runtime_error(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in message for marker in (
        "cuda", "cudnn", "cublas", "curand", "cufft", "gpu device",
        "compute capability", "no kernel image", "driver version",
    ))


@lru_cache(maxsize=1)
def _load_whisper_model(model_name: str, device: str, compute: str):
    """Keep at most one backend/model resident per process."""
    return _create_whisper_model(model_name, device, compute)


def _create_whisper_model(model_name: str, device: str, compute: str):
    from faster_whisper import WhisperModel
    return WhisperModel(model_name, device=device, compute_type=compute)


def _transcribe_with_backend(audio_path: Path, model_name: str, device: str,
                             compute: str, language: str | None,
                             hotwords: list[str] | None) -> dict:
    model = _load_whisper_model(model_name, device, compute)
    segments, info = model.transcribe(str(audio_path), word_timestamps=True,
                                      vad_filter=False, language=language,
                                      hotwords=", ".join(hotwords) if hotwords else None)
    words = []
    recognized = []
    for segment in segments:
        recognized.append(str(segment.text or ""))
        for word in segment.words or []:
            words.append({"text": str(word.word), "start": float(word.start), "end": float(word.end)})
    return {"text": "".join(recognized), "words": words,
            "model": model_name, "device": device, "compute_type": compute,
            "language": getattr(info, "language", None),
            "model_download_timeout": _hf_hub_download_timeout()}


def _preferred_backend() -> tuple[str, str]:
    try:
        from ctranslate2 import get_cuda_device_count
        if get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _hf_hub_download_timeout() -> str:
    """Report external HF_HUB_DOWNLOAD_TIMEOUT or the Hugging Face Hub 10s default."""
    return os.environ.get("HF_HUB_DOWNLOAD_TIMEOUT", HF_HUB_DOWNLOAD_TIMEOUT_DEFAULT_S)


def _validate_words(raw: Any, duration_us: int) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("words")
    if not isinstance(raw, list) or not raw:
        raise ValueError("ASR returned no word timestamps")
    words = []
    previous = 0
    for item in raw:
        text = str(_get(item, "text", _get(item, "word", ""))).strip()
        start = round(float(_get(item, "start", 0)) * 1_000_000)
        end = round(float(_get(item, "end", 0)) * 1_000_000)
        if not text or start < 0 or start >= duration_us or end <= start or end > duration_us + 20_000:
            raise ValueError(f"invalid ASR word interval: {item!r}")
        if words and start < previous:
            raise ValueError("ASR word timestamps overlap or are out of order")
        words.append({"text": text, "start_us": start, "end_us": min(end, duration_us)})
        previous = end
    return words


def _group_words(words: list[dict], brands: list[str]) -> list[dict]:
    protected = tuple(sorted(set(KEEP_WORDS) | {x for x in brands if x}, key=len, reverse=True))
    # Merge adjacent ASR tokens that together form a protected brand/whole word.
    merged = []
    i = 0
    while i < len(words):
        best_n = 1
        joined = words[i]["text"]
        for n in range(2, min(8, len(words) - i) + 1):
            joined += words[i+n-1]["text"]
            if any(joined.replace(" ", "").casefold() == term.replace(" ", "").casefold() for term in protected):
                best_n = n
        subset = words[i:i+best_n]
        merged.append({"text": "".join(w["text"] for w in subset), "start_us": subset[0]["start_us"], "end_us": subset[-1]["end_us"]})
        i += best_n

    cues, current = [], []
    for word in merged:
        candidate = current + [word]
        pause = current and word["start_us"] - current[-1]["end_us"] >= 420_000
        punct = current and re.search(r"[，。！？；,.!?;]$", current[-1]["text"])
        too_long = current and text_units("".join(w["text"] for w in candidate)) > 17
        if current and (pause or punct or too_long):
            cues.append(_cue(current)); current = []
        current.append(word)
    if current:
        cues.append(_cue(current))
    return cues


def _cue(words: list[dict]) -> dict:
    return {"text": "".join(w["text"] for w in words).strip(), "start_us": words[0]["start_us"], "end_us": words[-1]["end_us"]}


def _resolve_cached_model(model_name: str) -> Path | None:
    """Resolve aliases only from the existing Hub cache; this function never downloads."""
    path = Path(model_name).expanduser()
    if path.exists():
        return path.resolve()
    try:
        from faster_whisper.utils import download_model
        resolved = download_model(model_name, local_files_only=True)
        candidate = Path(resolved)
        return candidate.resolve() if candidate.exists() else None
    except Exception:
        return None


def _model_identity(model_name: str) -> dict:
    """Capture alias, resolved local snapshot revision, and key weight file identities."""
    resolved = _resolve_cached_model(model_name)
    if resolved is None:
        return {"requested_model": model_name, "available": False,
                "resolved_path": None, "snapshot_revision": None, "key_files": []}
    revision = resolved.name if resolved.parent.name == "snapshots" else None
    if resolved.is_file():
        files = [resolved]
        root = resolved.parent
    elif resolved.is_dir():
        root = resolved
        files = [child for child in resolved.rglob("*") if child.is_file()]
    else:
        files = []
        root = resolved
    key_names = {"model.bin", "config.json", "tokenizer.json", "tokenizer_config.json",
                 "preprocessor_config.json", "vocabulary.json", "vocabulary.txt", "merges.txt"}
    selected = [p for p in files if p.name.lower() in key_names]
    if not selected:
        selected = files
    identities = []
    for child in sorted(selected, key=lambda p: str(p).casefold()):
        try:
            info = child.stat()
            identities.append({"path": str(child.relative_to(root)), "size": info.st_size,
                               "mtime_ns": info.st_mtime_ns})
        except OSError:
            continue
    return {"requested_model": model_name, "available": bool(identities),
            "resolved_path": str(resolved), "snapshot_revision": revision,
            "key_files": identities}


def _model_fingerprint(model_name: str, identity: dict | None = None) -> str:
    identity = identity or _model_identity(model_name)
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _validate_cached_cues(raw: Any, duration_us: int) -> bool:
    if not isinstance(raw, list) or not raw:
        return False
    previous_end = -1
    for cue in raw:
        try:
            text, start, end = str(cue["text"]).strip(), int(cue["start_us"]), int(cue["end_us"])
        except (KeyError, TypeError, ValueError):
            return False
        if not text or start < 0 or end <= start or end > duration_us or start < previous_end:
            return False
        if text_units(text) > 30:
            return False
        previous_end = end
    return True


def _validate_cached_words(raw: Any, duration_us: int) -> bool:
    if not isinstance(raw, list) or not raw:
        return False
    previous_end = -1
    for word in raw:
        try:
            text, start, end = str(word["text"]).strip(), int(word["start_us"]), int(word["end_us"])
        except (KeyError, TypeError, ValueError):
            return False
        if not text or start < 0 or end <= start or end > duration_us or start < previous_end:
            return False
        previous_end = end
    return True


def build_captions(manifest: dict, clips: list, report_dir: Path, ffmpeg: str,
                   *, audio_overrides: dict | None = None,
                   transcribe: Callable[..., dict] | None = None) -> dict:
    report_dir = Path(report_dir)
    issues: list[str] = []
    recognized = ""
    validated_words: list[dict] = []
    report_path = report_dir / "audio_captions.json"
    model_name = str(manifest.get("asr_model") or os.environ.get("JY_ASR_MODEL", "small"))
    try:
        raw_timeout = manifest.get("asr_timeout_s", ASR_TIMEOUT_DEFAULT_S)
        if isinstance(raw_timeout, bool):
            raise ValueError("asr_timeout_s must be a finite positive number")
        timeout_s = float(raw_timeout)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("asr_timeout_s must be a finite positive number")
        for clip in clips:
            mode = str(_get(clip, "audio_mode", "tts") or "tts").lower()
            text = str(_get(clip, "text", "") or "").strip()
            if text and mode not in {"tts", "file"}:
                raise ValueError(f"clip with narration text uses forbidden audio mode '{mode}'")
        audio, audio_identity = _timeline_audio(clips, report_dir, ffmpeg, audio_overrides)
        reference = _reference_text(manifest, clips)
        brands = manifest.get("brand_names") or manifest.get("subtitle_brands") or []
        if isinstance(brands, str):
            brands = [brands]
        brands = sorted({str(x) for x in brands if str(x).strip()})
        hotwords = _asr_hotwords(reference, {**manifest, "brand_names": brands})
        model_identity = _model_identity(model_name)
        cache_key = {"audio_sha256": audio_identity["sha256"], "requested_model": model_name,
                     "adapter": ASR_VERSION, "model_identity": model_identity,
                     "model_fingerprint": _model_fingerprint(model_name, model_identity),
                     "settings": {"device": _preferred_backend()[0],
                                  "compute_type": _preferred_backend()[1],
                                  "language": manifest.get("asr_language"),
                                  "download_timeout": _hf_hub_download_timeout(),
                                  "hotwords": hotwords,
                                  "timeout_s": timeout_s},
                     "keep_words_sha256": hashlib.sha256("\n".join(KEEP_WORDS).encode("utf-8")).hexdigest(),
                     "brand_names": brands}
        cache_key["reference_sha256"] = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        cache_path = report_dir / "audio_captions.cache.json"
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                identity = cached.get("identity") or {}
                valid_cues = _validate_cached_cues(cached.get("cues"), int(audio_identity["duration_us"]))
                valid_words = _validate_cached_words(cached.get("asr_word_timestamps"), int(audio_identity["duration_us"]))
                valid_identity = (identity.get("audio_sha256") == cache_key["audio_sha256"]
                                  and identity.get("requested_model") == cache_key["requested_model"]
                                  and identity.get("model_identity") == cache_key["model_identity"]
                                  and identity.get("model_fingerprint") == cache_key["model_fingerprint"]
                                  and identity.get("adapter") == cache_key["adapter"]
                                  and identity.get("reference_sha256") == cache_key["reference_sha256"]
                                  and identity.get("settings") == cache_key["settings"]
                                  and identity.get("device") == cache_key["settings"]["device"]
                                  and identity.get("compute_type") == cache_key["settings"]["compute_type"])
                if (model_identity.get("available") and cached.get("cache_key") == cache_key
                        and valid_cues and valid_words and valid_identity):
                    report = {"ok": True, "cues": cached["cues"], "identity": identity,
                              "issues": [], "asr_text": cached.get("asr_text", ""),
                              "asr_word_timestamps": cached["asr_word_timestamps"],
                              "reference_text": reference, "audio": audio_identity,
                              "cache_hit": True, "status": "recognized_cached"}
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    return {k: report[k] for k in ("ok", "cues", "identity", "issues")}
            except (ValueError, OSError):
                pass
        if transcribe is not None:
            result = transcribe(audio, model_name=model_name,
                                language=manifest.get("asr_language"),
                                hotwords=hotwords)
        else:
            result = _transcribe_in_child(audio, model_name=model_name,
                                          language=manifest.get("asr_language"),
                                          hotwords=hotwords, timeout_s=timeout_s)
        if not isinstance(result, dict):
            raise ValueError("ASR adapter must return a dict with text and timestamped words")
        # The initial cache-only lookup cannot see an uncached alias. Resolve again
        # locally after the child has downloaded/loaded its model.
        model_identity = _model_identity(model_name)
        cache_key["model_identity"] = model_identity
        cache_key["model_fingerprint"] = _model_fingerprint(model_name, model_identity)
        if transcribe is None and not model_identity.get("available"):
            raise ValueError("ASR succeeded but its local model snapshot identity could not be resolved; refusing to cache")
        validated_words = _validate_words(result.get("words"), int(audio_identity["duration_us"]))
        recognized = str(result.get("text") or "".join(w["text"] for w in validated_words))
        word_text = "".join(w["text"] for w in validated_words)
        word_alignment = SequenceMatcher(None, _clean(recognized), _clean(word_text)).ratio()
        if not word_text.strip() or word_alignment < 0.75:
            raise ValueError(f"ASR transcript/word timestamp text mismatch requires review (similarity={word_alignment:.3f})")
        similarity = (SequenceMatcher(None, _clean(_comparison_text(reference)),
                                      _clean(_comparison_text(recognized))).ratio()
                      if reference else 1.0)
        if not recognized.strip():
            raise ValueError("ASR recognized empty text")
        if reference and similarity < CONTENT_SIMILARITY_MIN:
            raise ValueError(f"ASR/reference mismatch requires review (similarity={similarity:.4f}, minimum={CONTENT_SIMILARITY_MIN:.2f})")
        normalized_reference, normalized_asr = _clean(reference), _clean(recognized)
        protected_terms = set(hotwords)
        missing_terms = sorted(term for term in protected_terms
                               if _clean(term) and _clean(term) in normalized_reference
                               and _clean(term) not in normalized_asr)
        if missing_terms:
            raise ValueError(f"ASR/reference protected-word mismatch requires review: {missing_terms}")
        ref_numbers = re.findall(r"\d+(?:\.\d+)?", reference)
        asr_numbers = re.findall(r"\d+(?:\.\d+)?", recognized)
        ref_numbers = [_canonical_number(value) for value in re.findall(_NUM_TOKEN, reference)]
        asr_numbers = [_canonical_number(value) for value in re.findall(_NUM_TOKEN, recognized)]
        if ref_numbers != asr_numbers:
            raise ValueError(f"ASR/reference number mismatch requires review: {ref_numbers} != {asr_numbers}")
        cues = _group_words(validated_words, brands)
        if not cues:
            raise ValueError("ASR produced no usable cues")
        too_long = [cue for cue in cues if text_units(cue["text"]) > 30]
        if too_long:
            raise ValueError("ASR produced an unsplittable subtitle over 30 text units")
        if any(not c["text"] or c["start_us"] < 0 or c["end_us"] <= c["start_us"] or c["end_us"] > audio_identity["duration_us"] for c in cues):
            raise ValueError("generated cue has invalid or out-of-range interval")
        identity = {"audio_sha256": audio_identity["sha256"], "model": result.get("model", model_name),
                    "requested_model": model_name,
                    "model_identity": cache_key["model_identity"],
                    "model_fingerprint": cache_key["model_fingerprint"],
                    "device": result.get("device", "injected"), "compute_type": result.get("compute_type", "injected"),
                    "language": result.get("language"), "adapter": ASR_VERSION,
                    "reference_sha256": hashlib.sha256(reference.encode("utf-8")).hexdigest(),
                    "audio_duration_us": audio_identity["duration_us"]}
        identity["settings"] = cache_key["settings"]
        if result.get("fallback"):
            issues.append(str(result["fallback"]))
        report = {"ok": True, "cues": cues, "identity": identity, "issues": issues,
                  "asr_text": recognized, "reference_text": reference, "reference_similarity": round(similarity, 4),
                  "asr_word_timestamps": validated_words,
                  "audio": audio_identity, "cache_hit": False, "status": "recognized"}
        cache_path.write_text(json.dumps({"cache_key": cache_key, "identity": identity,
                                          "asr_text": recognized, "asr_word_timestamps": validated_words,
                                          "cues": cues},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # fail closed: never keep a previous or estimated subtitle result
        if isinstance(exc, ASRTimeoutError):
            issues.append(str(exc))
        else:
            issues.append(f"caption_generation_failed: {type(exc).__name__}: {exc}")
        report = {"ok": False, "cues": [], "identity": {"model": model_name, "adapter": ASR_VERSION},
                  "issues": issues, "status": "timeout" if isinstance(exc, ASRTimeoutError) else "review_required",
                  "asr_text": recognized,
                  "asr_word_timestamps": validated_words,
                  "hotwords": locals().get("hotwords", []),
                  "audio": locals().get("audio_identity", {}),
                  "reference_sha256": hashlib.sha256(_reference_text(manifest, clips).encode("utf-8")).hexdigest()}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: report[k] for k in ("ok", "cues", "identity", "issues")}


def _worker_main() -> int:
    """Private module entry used only by the bounded recognition subprocess."""
    try:
        payload = json.load(sys.stdin)
        result = transcribe_faster_whisper(
            Path(payload["audio_path"]), model_name=payload.get("model_name"),
            language=payload.get("language"), hotwords=payload.get("hotwords"),
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "transcribe":
    raise SystemExit(_worker_main())
