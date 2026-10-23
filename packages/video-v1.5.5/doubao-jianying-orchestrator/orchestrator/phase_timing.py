"""动作相位打点（v1.5.2）：对命中场景规则的镜头密集抽帧，让视觉模型标出各阶段起止时间。

常规视觉分析每个镜头只看 mid / end 两帧，判断不了「倒水从哪一秒开始、纸巾擦完在哪一秒」。
吸水实验、撕包装、荧光测试这些要求完整动作的场景，在选定镜头后再单独看一遍：
在场景窗口内均匀抽最多 20 帧拼成一张图，模型只能从这些帧的时间里选起止点。
结果按素材签名 + 窗口 + 场景缓存，同一镜头不重复花钱。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from .file_safety import locked_json_update, private_temp_dir

PHASE_TIMING_VERSION = "phase-timing-v2"
MAX_FRAMES = 20
MIN_STEP_S = 0.4
EDGE_S = 0.05
DISABLE_ENV = "JY_PHASE_TIMING"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sample_times(start: float, end: float, *, max_frames: int = MAX_FRAMES) -> list[float]:
    lo, hi = start + EDGE_S, end - EDGE_S
    if hi <= lo:
        return [round((start + end) / 2.0, 3)]
    count = max(2, min(int(max_frames), int((hi - lo) / MIN_STEP_S) + 1))
    step = (hi - lo) / (count - 1)
    return [round(lo + i * step, 3) for i in range(count)]


def _cache_path(report_dir: Path) -> Path:
    configured = str(os.environ.get("JY_VISION_CACHE_DIR") or "").strip()
    root = Path(configured) if configured else Path(report_dir).parent.parent / ".cache"
    return root / "phase_timing.json"


def _cache_key(shot: dict[str, Any], scene: dict[str, Any], profile: str) -> str:
    source = str(shot.get("video") or "").strip()
    try:
        path = Path(source).expanduser().resolve()
        stat = path.stat()
        signature = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
        source = str(path)
    except OSError:
        return ""
    identity = {
        "version": PHASE_TIMING_VERSION, "profile": profile, "video": source,
        "signature": signature, "scene": scene.get("id"),
        "phases": [p.get("name") for p in scene.get("phases") or []],
        "start_us": int(round(_number(shot.get("source_start")) * 1e6)),
        "end_us": int(round(_number(shot.get("source_end")) * 1e6)),
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_prompt(scene: dict[str, Any], times: list[float]) -> str:
    tiles = "\n".join(f"- 图块 {i + 1}：t={t:.2f}s" for i, t in enumerate(times))
    phases = "\n".join(f"- {p['name']}（{p.get('label', '')}）：{p.get('describe', '')}"
                       for p in scene.get("phases") or [])
    names = ", ".join(p["name"] for p in scene.get("phases") or [])
    return f"""你在给一段「{scene.get('label', '')}」素材标注动作阶段。图片是按时间顺序排列的抽帧拼图
（从左到右、从上到下），每个图块对应的素材时间如下：
{tiles}

需要标注的阶段：
{phases}

对每个阶段，给出它**最早出现**的图块时间作为 start、**最后出现**的图块时间作为 end。
时间只能从上面的图块时间里选，不要编造。画面里没出现的阶段不要输出，并把 complete 设为 false。
complete 只有在所有阶段（{names}）都完整出现时才为 true。
严格只输出 JSON：{{"complete": true, "phases": [{{"name": "...", "start": 0.0, "end": 0.0}}], "note": ""}}
"""


def validate(payload: Any, scene: dict[str, Any], times: list[float],
             bounds: Optional[tuple[float, float]] = None) -> dict[str, Any]:
    """把模型输出收紧成可用的阶段区间；起止各向外扩半个抽帧间隔，保证动作不被截掉。

    ``bounds`` 是镜头自身的起止，外扩不能越过它。
    """
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "PHASE_RESPONSE_INVALID"}
    expected = [p["name"] for p in scene.get("phases") or []]
    if not times:
        return {"ok": False, "reason": "PHASE_NO_FRAMES"}
    step = (times[-1] - times[0]) / max(1, len(times) - 1) if len(times) > 1 else 0.5
    lo_limit, hi_limit = times[0] - step / 2.0, times[-1] + step / 2.0
    if bounds is not None:
        lo_limit, hi_limit = max(lo_limit, float(bounds[0])), min(hi_limit, float(bounds[1]))
    phases: dict[str, list[float]] = {}
    for row in payload.get("phases") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name not in expected or name in phases:
            continue
        start, end = _number(row.get("start"), -1.0), _number(row.get("end"), -1.0)
        if start < times[0] - 0.01 or end > times[-1] + 0.01 or end < start:
            return {"ok": False, "reason": "PHASE_TIME_OUT_OF_RANGE", "phase": name}
        phases[name] = [round(max(lo_limit, start - step / 2.0), 3),
                        round(min(hi_limit, end + step / 2.0), 3)]
    present = [n for n in expected if n in phases]
    for first, second in zip(present, present[1:]):
        if phases[second][0] + 1e-6 < phases[first][0]:
            return {"ok": False, "reason": "PHASE_ORDER_INVALID", "phases": phases}
    complete = bool(payload.get("complete")) and len(present) == len(expected)
    return {"ok": bool(present), "complete": complete, "phases": phases,
            "step_s": round(step, 3), "note": str(payload.get("note") or "")[:200],
            "reason": "" if complete else "PHASE_INCOMPLETE"}


def locate(shot: dict[str, Any], scene: dict[str, Any], report_dir: Path, *,
           ffmpeg: Optional[str] = None, profile: Optional[str] = None, timeout: int = 240,
           runner: Optional[Callable[[Path, str], tuple[bool, str]]] = None) -> dict[str, Any]:
    if str(os.environ.get(DISABLE_ENV, "1")).strip() == "0":
        return {"ok": False, "reason": "PHASE_TIMING_DISABLED"}
    from . import shot_analyzer, vision_analyzer
    profile = vision_analyzer._clean_profile(
        profile if profile is not None else os.environ.get("JY_VISION_PROFILE", "volc"))
    start = _number(shot.get("source_start"))
    end = _number(shot.get("source_end"), start)
    times = sample_times(start, end)
    cache_path = _cache_path(report_dir)
    key = _cache_key(shot, scene, profile)
    if key:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8")).get(key)
        except (OSError, ValueError, AttributeError):
            cached = None
        if isinstance(cached, dict) and cached.get("version") == PHASE_TIMING_VERSION:
            return {**cached, "cached": True}
    video = Path(str(shot.get("video") or ""))
    with private_temp_dir(prefix="jy_phase_", dir=report_dir) as temp:
        temp_dir = Path(temp)
        items = []
        for index, t in enumerate(times, 1):
            frame = temp_dir / f"f_{index:03d}.jpg"
            if shot_analyzer.extract_frame(video, t, frame, ffmpeg=ffmpeg):
                items.append({"path": str(frame), "label": f"t={t:.2f}s", "timestamp": t})
        if len(items) < 2:
            return {"ok": False, "reason": "PHASE_FRAME_EXTRACT_FAILED"}
        times = [item["timestamp"] for item in items]
        sheet = temp_dir / "phase_sheet.jpg"
        if not vision_analyzer._write_sheet(items, sheet, columns=5, ffmpeg=ffmpeg):
            return {"ok": False, "reason": "PHASE_SHEET_FAILED"}
        prompt = build_prompt(scene, times)
        if runner is not None:
            ok, text = runner(sheet, prompt)
        else:
            ok, text = vision_analyzer._run_vision_with_retry(
                sheet, prompt, temp_dir / "phase_response.txt", profile=profile,
                timeout=timeout, workdir=temp_dir, max_retries=2)
    if not ok:
        return {"ok": False, "reason": "PHASE_VISION_FAILED", "message": str(text)[-400:]}
    try:
        payload = vision_analyzer._json_from_text(text)
    except (ValueError, TypeError):
        return {"ok": False, "reason": "PHASE_RESPONSE_INVALID", "message": str(text)[-400:]}
    result = validate(payload, scene, times, bounds=(start, end))
    result.update({"version": PHASE_TIMING_VERSION, "scene": scene.get("id"),
                   "sample_times": times, "cached": False})
    if key and result.get("reason") not in ("PHASE_RESPONSE_INVALID",):
        def mutate(data):
            data = data if isinstance(data, dict) else {}
            data[key] = {k: v for k, v in result.items() if k != "cached"}
            return data
        try:
            locked_json_update(cache_path, mutate, default={})
        except (OSError, TimeoutError, ValueError):
            pass
    return result
