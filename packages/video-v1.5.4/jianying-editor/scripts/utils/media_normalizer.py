import os
import sys
from typing import Optional


def _norm_output_path(input_path: str) -> str:
    abs_in = os.path.abspath(input_path)
    parent = os.path.dirname(abs_in)
    stem, _ = os.path.splitext(os.path.basename(abs_in))
    cache_dir = os.path.join(parent, "__jycache__")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{stem}.__jy_norm__.mp4")


def _is_cache_fresh(src: str, dst: str) -> bool:
    if not os.path.exists(dst):
        return False
    try:
        return os.path.getmtime(dst) >= os.path.getmtime(src)
    except OSError:
        return False


def _encoder_api():
    """复用编排器的 GPU/CPU 编码选择，避免这里再写一套 libx264 参数。"""
    try:
        from orchestrator.platform_env import find_ffmpeg
        from orchestrator.video_encode import run_h264_encode
    except ImportError:
        root = os.path.abspath(os.path.join(
            os.path.dirname(__file__), os.pardir, os.pardir, os.pardir,
            "doubao-jianying-orchestrator"))
        if root not in sys.path:
            sys.path.insert(0, root)
        from orchestrator.platform_env import find_ffmpeg
        from orchestrator.video_encode import run_h264_encode
    ffmpeg_bin = "ffmpeg"
    try:
        found, _ffprobe = find_ffmpeg()
        if found:
            ffmpeg_bin = str(found)
    except Exception:
        pass
    return run_h264_encode, ffmpeg_bin


def _gpu_decode_prefix(ffmpeg_bin: str) -> list:
    """该 ffmpeg 声明 cuda 且显存够用时返回 ``-threads 1 -hwaccel cuda``。

    不加 ``-hwaccel_output_format cuda``：帧留在系统内存，CPU 滤镜照常可用。
    编排器导入失败、JY_GPU_DISABLE 或探测出错一律返回空列表。
    """
    try:
        from orchestrator import gpu_runtime
        from orchestrator.video_decode import gpu_decode_args
        if gpu_runtime.gpu_disabled() or not gpu_runtime.vram_ok():
            return []
        return list(gpu_decode_args(ffmpeg_bin))
    except Exception:
        return []


def normalize_webm_for_jianying(input_path: str) -> Optional[str]:
    """
    Convert WEBM to JianYing-friendly MP4 before timeline import.

    Output profile:
    - Video: H.264 yuv420p，优先 GPU（h264_nvenc / h264_amf / h264_qsv），否则 libx264
    - Decode: 有 cuda 时 ``-threads 1 -hwaccel cuda``，整次失败再去掉硬件解码重跑
    - Audio: AAC (optional if source has audio)
    """
    src = os.path.abspath(input_path)
    if not os.path.exists(src):
        return None

    dst = _norm_output_path(src)
    if _is_cache_fresh(src, dst):
        return dst

    try:
        run_h264_encode, ffmpeg_bin = _encoder_api()
        decode_prefix = _gpu_decode_prefix(ffmpeg_bin)

        def encode(prefix: list) -> None:
            run_h264_encode(
                ffmpeg_bin,
                [
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    *prefix,
                    "-i",
                    src,
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                ],
                dst,
                trailing=["-c:a", "aac", "-b:a", "192k"],
            )

        if decode_prefix:
            try:
                encode(decode_prefix)
            except FileNotFoundError:
                raise
            except Exception as e:
                print(f"⚠️ WEBM GPU decode failed, retrying on CPU: {e}")
                encode([])
        else:
            encode([])
    except FileNotFoundError:
        print("❌ FFmpeg not found. Cannot normalize WEBM for JianYing import.")
        return None
    except Exception as e:
        print(f"❌ WEBM normalization failed: {e}")
        return None

    if not os.path.exists(dst) or os.path.getsize(dst) <= 0:
        print("❌ WEBM normalization failed: empty output")
        return None
    return dst
