"""Explicit Qwen video input; no contact-sheet or transport fallback."""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path

from .. import ai_editor_contract as contract
from ..runtime.file_safety import atomic_write_text, file_lock
from . import vision_analyzer, vision_client

PROTOCOL = "qwen-temporal-video-v1"


def validate_milestones(value, duration_us):
    """Bounds and schema only; plausible timestamps still need frame review."""
    if type(duration_us) is not int or duration_us <= 0 or not isinstance(value, dict):
        return False
    if (not isinstance(value.get("description"), str) or not value["description"].strip()
            or type(value.get("result_visible")) is not bool
            or not isinstance(value.get("limitations"), str)):
        return False
    rows = value.get("milestones")
    window = value.get("complete_window_us")
    if not isinstance(rows, list) or not rows or not isinstance(window, dict):
        return False
    left, right = window.get("start"), window.get("end")
    if type(left) is not int or type(right) is not int or not 0 <= left < right <= duration_us:
        return False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
            return False
        start, end = row.get("start_us"), row.get("end_us")
        if type(start) is not int or type(end) is not int or not left <= start < end <= right:
            return False
    return True


def ground(video, question, output, *, duration_us, fps=10):
    """Ask for bounded local-video milestones, without a target edit duration."""
    if type(duration_us) is not int or duration_us <= 0:
        raise ValueError("TEMPORAL_VIDEO_DURATION_INVALID")
    prompt = f"""只依据实际视频，定位需要的完整动作和清楚结果。问题：{question}
视频真实长度为{duration_us}微秒；从视频0秒起，1秒=1000000微秒。
若画面有真实时钟，读钟辅助定位。所有时间必须在视频范围内；不要猜测未出现的动作。
不要为凑时长截掉必要动作或结果，不必保留重复演示或结果之后长期无变化。
只输出JSON {{"description":"实际可见事实","milestones":[{{"name":"实际事件",
"start_us":0,"end_us":1000000}}],"complete_window_us":{{"start":0,"end":1000000}},
"result_visible":true,"limitations":"看不到或不能证明的内容"}}。
整数微秒，complete_window_us包含所有事件；缺少所需结果时result_visible=false。
结果只是采样观察，不代表帧级精度或成片验收。"""
    return review(video, prompt, output, fps=fps,
                  validator=lambda value: validate_milestones(value, duration_us))


def review(video, prompt, output, *, fps=10, timeout=180, validator=None):
    """Review the actual supplied MP4, retaining its own zero-based clock.

    The caller owns preparation, original-source mapping and semantic validation.
    Success means a valid response, never content acceptance. The service may
    sample fewer frames; requested FPS is recorded rather than claimed observed.
    """
    if type(fps) not in (int, float) or not math.isfinite(fps) or not .1 <= fps <= 10:
        raise ValueError("TEMPORAL_VIDEO_FPS_INVALID")
    if vision_analyzer._vision_transport() != "qwen":
        raise ValueError("TEMPORAL_VIDEO_REQUIRES_QWEN")
    cfg = vision_analyzer._qwen_settings()
    if not all(cfg.values()):
        raise ValueError("TEMPORAL_VIDEO_ROUTE_MISSING")
    path, output = Path(video), Path(output)
    if path.resolve() == output.resolve() or any(path.resolve() == candidate.resolve()
            for candidate in (output.with_suffix(".input.json"), output.with_suffix(".call.json"),
                              output.with_suffix(".video"))):
        raise ValueError("TEMPORAL_VIDEO_OUTPUT_COLLIDES_WITH_INPUT")
    if path.suffix.lower() != ".mp4":
        raise ValueError("TEMPORAL_VIDEO_REQUIRES_MP4")
    before = contract.source_identity(path)
    if not 0 < before["size"] <= 20_000_000:
        raise ValueError("TEMPORAL_VIDEO_SIZE_LIMIT")
    content = path.read_bytes()
    identity = {"protocol": PROTOCOL, "input_kind": "video_url", "source": before,
                "video_sha256": hashlib.sha256(content).hexdigest(), "requested_fps": fps,
                "prompt_sha256": contract.digest(prompt), "model": cfg["model"],
                "endpoint": cfg["base_url"]}
    if contract.source_identity(path) != before:
        raise ValueError("TEMPORAL_VIDEO_INPUT_CHANGED")
    output.parent.mkdir(parents=True, exist_ok=True)
    def valid(value):
        return isinstance(value, dict) and (validator is None or validator(value) is not False)
    # The shared client owns account slots, deadlines, JSON validation and retry
    # ledgers, including successful-response replay and bounded recovery.
    body = {"model": cfg["model"], "enable_thinking": False, "temperature": 0,
            "max_tokens": 5000, "messages": [{"role": "user", "content": [
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," +
                    base64.b64encode(content).decode("ascii")}, "fps": fps},
                {"type": "text", "text": prompt}]}]}
    with file_lock(output.with_suffix(".video"), timeout=600):
        result = vision_client.post_chat(cfg["base_url"] + "/chat/completions", cfg["api_key"],
            body, output, timeout=timeout, max_attempts=2, validator=valid)
        if contract.source_identity(path) != before:
            raise ValueError("TEMPORAL_VIDEO_INPUT_CHANGED")
        atomic_write_text(output.with_suffix(".input.json"), json.dumps(identity, ensure_ascii=False, indent=2))
        if not result.ok:
            raise ValueError("TEMPORAL_VIDEO_CALL_FAILED:" + result.error_code)
        return vision_analyzer._json_from_text(result.text)
