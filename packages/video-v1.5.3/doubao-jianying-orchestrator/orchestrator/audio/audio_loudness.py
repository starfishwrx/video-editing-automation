"""旁白响度归一（通用剪辑处理规则，2026-09-17 用户定案）。

## 要解决的问题

TTS 逐条合成，各条自带不同电平。R3 实测 11 条旁白的 Integrated 响度从
-37.12 到 -21.22 LUFS —— **跨度 15.9 LU**。铺到同一条 Narration 轨上，成片
听起来就是「忽大忽小」。这不是某条视频的特例，是逐条 TTS 的固有属性，所以
按用户定案做成**通用规则**，对每条旁白一律执行。

## 为什么不用 loudnorm

两遍 `loudnorm`（带 measured_* 的线性模式）在真实数据上**不成立**：真峰值已经
到 -2.13 dBTP 的那几条，按 -16 LUFS 反推需要的增益会顶破 -1.5 dBTP 上限，
ffmpeg 于是静默回退到动态模式，输出仍散在 -21.90 ~ -16.02（5.88 LU）。实测见下。

改用「**纯增益 + 限幅**」：先按 Integrated 差值定增益，再用 `alimiter` 把峰值
钉在 TP_LIMIT 上。实测 11 条：

    输入跨度  15.90 LU  (-37.12 .. -21.22)
    输出跨度   1.88 LU  (-17.88 .. -16.00)
残余响度差来自限幅器对最响几条的削峰。实际输出响度随输入而变，记录测量值。

## 为什么时长必须钉死

44.1kHz AAC 后处理会把原 48kHz TTS 的采样边界量化到另一时钟，导致整数微秒
时长与选镜计划差几微秒。内部旁白资产改用 48kHz PCM WAV，以源时长对应的采样数
执行 apad/atrim；写入层按同一采样边界校验，不放宽真实缺失音频的检查。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional
import hashlib
import uuid
import wave

from ..media.probe import probe_media
from ..runtime.file_safety import file_lock

SAMPLE_RATE = 48000
NORMALIZATION_VERSION = "narration-48k-pcm-v1"


def normalized_path(src: Path, directory: Path, *, target_lufs: float = -16.0,
                    tp_limit_db: float = -1.5, max_gain_db: float = 24.0) -> Path:
    """Invalidate processed audio only; preserve the original cloud TTS cache."""
    src = Path(src).resolve(strict=True)
    stat = src.stat()
    identity = [str(src), stat.st_size, stat.st_mtime_ns, NORMALIZATION_VERSION,
                SAMPLE_RATE, "pcm_s16le", target_lufs, tp_limit_db, max_gain_db]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]
    return Path(directory) / f"{src.stem}-{key}.wav"


def _audio_facts(path: Path, ffprobe: str) -> dict:
    facts = probe_media(path, ffprobe=ffprobe)
    audio = next((s for s in facts["streams"] if s.get("codec_type") == "audio"), None)
    if not audio or any(s.get("codec_type") == "video" for s in facts["streams"]):
        raise ValueError("NARRATION_AUDIO_ONLY_REQUIRED")
    if path.suffix.lower() == ".wav" and str(audio.get("codec_name", "")).startswith("pcm_"):
        try:
            with wave.open(str(path), "rb") as stream:
                remaining = stream.getnframes()
                frame_bytes = stream.getnchannels() * stream.getsampwidth()
                while remaining:
                    count = min(remaining, 65536)
                    if len(stream.readframes(count)) != count * frame_bytes:
                        raise ValueError("NARRATION_PCM_DATA_TRUNCATED")
                    remaining -= count
        except wave.Error as exc:
            raise ValueError("NARRATION_PCM_FORMAT_INVALID") from exc
    return {**facts, "sample_rate": int(audio.get("sample_rate") or 0),
            "channels": int(audio.get("channels") or 0)}


def _samples(duration_us: int) -> int:
    return (duration_us * SAMPLE_RATE + 500_000) // 1_000_000

# 旁白目标响度：短视频人声常用 -16 LUFS（YouTube/抖音投放口径的常见取值）。
TARGET_LUFS = -16.0
# 真峰值上限：-1.5 dBTP 给编码器留出 inter-sample 余量，避免导出后削顶。
TP_LIMIT_DB = -1.5
# 增益上限：防止把近乎静音的废条放大成底噪（-37 LUFS 那条需要 +21dB）。
MAX_GAIN_DB = 24.0
# 输入低于此值视为废条，不参与归一（放大只会得到噪声）。
MIN_INPUT_LUFS = -50.0


def _loudnorm_summary(ffmpeg: str, src: Path) -> Optional[dict]:
    """跑一遍 loudnorm 的测量模式，取回 JSON 汇总（不改动文件）。"""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(src),
         "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def measure_integrated_lufs(ffmpeg: str, src: Path) -> Optional[float]:
    summary = _loudnorm_summary(ffmpeg, src)
    if not summary:
        return None
    try:
        return float(summary["input_i"])
    except (KeyError, TypeError, ValueError):
        return None


def normalize_narration(src: Path, dst: Path, *, ffmpeg: str = "ffmpeg",
                        ffprobe: str = "ffprobe",
                        target_lufs: float = TARGET_LUFS,
                        tp_limit_db: float = TP_LIMIT_DB,
                        max_gain_db: float = MAX_GAIN_DB) -> dict:
    """Serialize one processed cache entry and publish only complete media."""
    with file_lock(dst, timeout=600):
        return _normalize_narration(src, dst, ffmpeg=ffmpeg, ffprobe=ffprobe,
            target_lufs=target_lufs, tp_limit_db=tp_limit_db, max_gain_db=max_gain_db)


def _normalize_narration(src: Path, dst: Path, *, ffmpeg: str, ffprobe: str,
                         target_lufs: float, tp_limit_db: float, max_gain_db: float) -> dict:
    """把一条旁白归一到 `target_lufs`，峰值钉在 `tp_limit_db`。

    内部调用使用48kHz PCM WAV。缓存按处理参数命名，复用前检查采样率与长度。
    """
    src = Path(src)
    dst = Path(dst)
    detail: dict = {"src": str(src), "dst": str(dst),
                    "target_lufs": target_lufs, "tp_limit_db": tp_limit_db,
                    "processing_version": NORMALIZATION_VERSION, "sample_rate": SAMPLE_RATE}
    source = _audio_facts(src, ffprobe)
    expected_samples = _samples(source["duration_us"])
    if dst.exists() and dst.stat().st_size > 0:
        try:
            cached = _audio_facts(dst, ffprobe)
            if (cached["sample_rate"] == SAMPLE_RATE and cached["channels"] == 2
                    and _samples(cached["duration_us"]) == expected_samples):
                detail.update(reused=True, out_lufs=measure_integrated_lufs(ffmpeg, dst),
                              output_duration_us=cached["duration_us"], sample_count=expected_samples)
                return detail
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    summary = _loudnorm_summary(ffmpeg, src)
    if not summary:
        detail["skipped"] = "measure_failed"
        return detail
    try:
        in_lufs = float(summary["input_i"])
        in_tp = float(summary["input_tp"])
    except (KeyError, TypeError, ValueError):
        detail["skipped"] = "measure_unparsable"
        return detail
    detail.update(in_lufs=in_lufs, in_tp=in_tp)
    if in_lufs <= MIN_INPUT_LUFS:
        detail["skipped"] = "below_floor"
        return detail

    gain_db = min(target_lufs - in_lufs, max_gain_db)
    limit = 10 ** (tp_limit_db / 20.0)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 限幅器 level=disabled：只削峰，不再叠加自动增益，否则响度会被二次改动。
    af = f"volume={gain_db:.3f}dB,alimiter=limit={limit:.6f}:level=disabled"
    # 使用统一 48kHz 的整数采样数，避免跨采样率量化和有损编码延迟。
    src_duration = source["duration_us"] / 1_000_000
    if src_duration:
        af += f",aresample={SAMPLE_RATE},apad=whole_len={expected_samples},atrim=end_sample={expected_samples}"
        detail["pinned_duration_s"] = round(src_duration, 6)
        # 接缝微淡变（用户 2026-09-17 反馈：分镜衔接处旁白不连贯）。逐句 TTS 是
        # 硬切的：起音辅音/尾音截断在镜头切换点上听着像「跳了一下」。20/30ms 的
        # 微淡变在人耳的响度感知之下，但足以消除硬切爆点 —— 这是广播级的标准做法。
        FADE_IN_S, FADE_OUT_S = 0.020, 0.030
        out_start = max(0.0, src_duration - FADE_OUT_S)
        af += f",afade=t=in:st=0:d={FADE_IN_S},afade=t=out:st={out_start:.6f}:d={FADE_OUT_S}"
        detail["fade_in_s"] = FADE_IN_S
        detail["fade_out_s"] = FADE_OUT_S
    codec = ["-c:a", "pcm_s16le"] if dst.suffix.lower() == ".wav" else ["-c:a", "aac", "-b:a", "192k"]
    temporary = dst.with_name(f".{dst.stem}-{uuid.uuid4().hex}{dst.suffix}")
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-af", af, *codec, "-ar", str(SAMPLE_RATE), "-ac", "2", str(temporary)],
            capture_output=True, text=True)
        if proc.returncode != 0 or not temporary.is_file():
            detail.update(skipped="encode_failed", stderr=(proc.stderr or "")[-400:])
            return detail
        actual = _audio_facts(temporary, ffprobe)
        if (actual["sample_rate"] != SAMPLE_RATE or actual["channels"] != 2
                or _samples(actual["duration_us"]) != expected_samples):
            raise ValueError("NARRATION_NORMALIZATION_FORMAT_OR_LENGTH_CHANGED")
        stat = src.stat()
        if (stat.st_size, stat.st_mtime_ns) != (source["size"], source["mtime_ns"]):
            raise ValueError("NARRATION_SOURCE_CHANGED_DURING_NORMALIZATION")
        detail.update(gain_db=round(gain_db, 3), out_lufs=measure_integrated_lufs(ffmpeg, temporary),
            output_duration_us=actual["duration_us"], sample_count=expected_samples,
            out_duration_s=actual["duration_us"] / 1_000_000,
            duration_delta_ms=(actual["duration_us"] - source["duration_us"]) / 1000)
        temporary.replace(dst)
        return detail
    finally:
        temporary.unlink(missing_ok=True)
