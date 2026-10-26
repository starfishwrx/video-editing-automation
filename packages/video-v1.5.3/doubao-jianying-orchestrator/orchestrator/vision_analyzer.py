"""Task-local image understanding and legacy description-to-shot matching.

Build labelled contact sheets and send actual image bytes to the configured
vision backend. The default is Volcengine Ark GLM-5.3-Flash
(``glm-5.3-flash``), not GLM-5.3 or ChatGLM3. Validate observations before
downstream selection and evidence checks. The default ``ai_editor`` selector
re-extracts candidate frames for its own pixel review; ``match_with_codex``
retains its historical name for compatibility, regardless of transport.

Only the current task's frames are sent.  Nothing is written to the reusable
material library.  A failed vision call remains a hard blocker; it is never
replaced by guessed tags.

v1.3.20: ``codex exec`` is an agent session, not a one-shot completion, and
that is what actually stalled the pipeline.  Given the shell and app tools,
the model answers a batch by cropping the contact sheet and re-viewing the
crops ("Let me crop to check details" -> powershell + PIL -> "Let me view
tiles 1-3"), a loop with no terminating condition.  Batches ran one to four
minutes and periodically crossed the 300 s timeout; before v1.3.17 the pipe
drain turned that into a permanent hang.  This version removes the tool
surface that feeds the loop and routes *every* codex call through one
file-backed runner so a timeout is always expressible as a process-tree kill.

Default transport is ``JY_VISION_TRANSPORT=volc_glm``: contact sheets post
straight to Volcengine Ark chat completions (``glm-5.3-flash``) with no Codex
agent process. Set ``codex`` / ``anthropic`` only as explicit rollback paths.
Batch and slot concurrency default to 20 (``JY_VISION_BATCH_WORKERS`` /
``JY_VISION_SLOTS``).
"""
from __future__ import annotations

import json
import base64
import hashlib
import math
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from .file_safety import locked_json_update, private_temp_dir
from .json_extract import extract_json, is_blank
from .proc_slots import env_float, env_int, slot


# Capture the imported source, rather than the file a later deployment may overwrite.
_RUNTIME_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


_DEMO_ROLES = ("direct_evidence", "usage_demo", "visual_metaphor",
               "product_display", "context", "CTA")
_DEMO_PHASES = ("prep", "perform", "result", "static")
VISION_ANALYSIS_VERSION = "product-visual-v3"
_VISION_CACHE_FIELDS = (
    "description", "visual_description", "visual_tags", "evidence_tags",
    "action_complete", "head_waste", "tail_waste", "analysis_confidence",
    "analysis_source", "status", "analysis_error", "action", "object",
    "result", "role", "evidence_strength", "action_phase", "setup_id",
    "has_subject", "readable_claims", "packaging_claims", "pointed_claims",
    "pointing_to_text", "explicit_pointing", "pointing_action", "gesture",
    "evidence_intervals", "action_segments", "action_complete_source", "text_pass",
    "text_agreed", "pointed_agreed", "rule_checks",
)
# A long take often holds several demonstrations (hang it, then pull one
# sheet); two frames per shot missed them, so frames follow the shot length.
# SAMPLE_MAX aligns with SHEET_TILES so one contact sheet can keep density on
# long takes without spilling into a second sheet. Override with JY_VISION_SAMPLE_MAX.
SAMPLE_STEP_S = 1.5
SAMPLE_MAX = 12
EVIDENCE_MAX_S = 2.5
SHEET_TILES = 12
_PORTRAIT_TILE = (360, 640)
_LANDSCAPE_TILE = (480, 270)
_TEXT_PORTRAIT_TILE = (720, 1280)
_TEXT_LANDSCAPE_TILE = (1280, 720)
_LABEL_H = 34


def _demo_action_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """从视觉结果里摘出 DEMO_ACTIONS V0.2 要求的素材侧字段。

    只在模型**确实给了值**时才写键 —— `demo_actions.judge()` 用「键不存在」
    区分「这是旧分析结果（放行但要标注）」和「模型明确说了主体不存在/没有
    可见结果（要拦）」。写 None 会把后者也归进旧结果，闸门就失效了。

    `result` 是例外：它显式为 null 是有意义的信息（只有动作、没有可见结果），
    所以模型给了 null 就写 null 并把强度压到 C —— 这正是「抚摸面层不得当
    事实证明」那条规则的落点。
    """
    out: dict[str, Any] = {}
    action = str(raw.get("action") or "").strip()
    if action:
        out["action"] = action
    obj = str(raw.get("object") or "").strip()
    if obj:
        out["object"] = obj
    if "result" in raw:
        result = raw.get("result")
        result = str(result).strip() if result is not None else None
        out["result"] = result or None
    role = str(raw.get("role") or "").strip()
    if role in _DEMO_ROLES:
        out["role"] = role
    strength = str(raw.get("evidence_strength") or "").strip().upper()[:1]
    if strength in ("A", "B", "C"):
        # result 为空时不得为 A（DEMO_ACTIONS_V0.2 §5 门禁）：
        # 没有可见结果就没有直接证据，模型高报也要压回去。
        if strength == "A" and not out.get("result"):
            strength = "C"
        out["evidence_strength"] = strength
    phase = str(raw.get("action_phase") or "").strip().lower()
    if phase in _DEMO_PHASES:
        out["action_phase"] = phase
    setup = str(raw.get("setup_id") or "").strip()
    if setup:
        out["setup_id"] = setup
    if "has_subject" in raw:
        out["has_subject"] = raw.get("has_subject") is True
    for key in ("readable_claims", "packaging_claims", "pointed_claims"):
        if key in raw:
            value = raw.get(key)
            if isinstance(value, (list, tuple)):
                out[key] = [str(item).strip() for item in value if str(item).strip()]
            elif str(value or "").strip():
                out[key] = [str(value).strip()]
    for key in ("pointing_to_text", "explicit_pointing"):
        if key in raw:
            out[key] = raw.get(key) is True
    for key in ("pointing_action", "gesture"):
        value = str(raw.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def _evidence_intervals(raw: Any, shot: dict[str, Any], frame: dict[str, Any] | None,
                        *, evidence: bool) -> list[dict[str, Any]]:
    """Keep model-provided evidence windows only when they are auditable.

    The vision call may return a tighter interval than the representative
    frame.  It is still bounded to the detected shot window and discarded when
    it is malformed.  A representative-frame fallback keeps older model
    responses useful while preserving a traceable source timestamp.
    """
    if not evidence:
        return []
    start = _number(shot.get("source_start"))
    end = max(start, _number(shot.get("source_end"), start))
    intervals: list[dict[str, Any]] = []
    values = raw if isinstance(raw, list) else [raw]
    for value in values:
        if not isinstance(value, dict):
            continue
        left = max(start, _number(value.get("start"), start))
        right = min(end, _number(value.get("end"), end))
        if right <= left:
            continue
        record = {"start": round(left, 3), "end": round(right, 3)}
        if value.get("frame_path"):
            record["frame_path"] = str(value["frame_path"])
        intervals.append(record)
    if intervals:
        return intervals
    if frame:
        center = _number(frame.get("timestamp"))
        return [{"start": round(max(start, center - 0.25), 3),
                 "end": round(min(end, center + 0.25), 3),
                 "frame_path": frame["path"], "frame_time": round(center, 3)}]
    return []


def _json_from_text(text: str) -> Any:
    """Parse a JSON array/object from a Codex final message.

    Tolerates ```json fences, bare arrays/objects and trailing prose; the
    first complete structured value wins.  Empty text raises
    ``EmptyResponseError`` (a ``ValueError``).
    """
    return extract_json(text)


def _load_image(path: Path, width: int, height: int):
    """Load/resize a frame with OpenCV, falling back to Pillow."""
    try:
        import cv2  # type: ignore
        image = cv2.imread(str(path))
        if image is None:
            return None
        return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
    except Exception:
        try:
            from PIL import Image  # type: ignore
            import numpy as np  # type: ignore
            image = Image.open(path).convert("RGB").resize((width, height))
            return np.asarray(image)[:, :, ::-1].copy()
        except Exception:
            return None


def _fit_size(width: int, height: int, box_w: int, box_h: int) -> tuple[int, int]:
    scale = min(box_w / max(1, width), box_h / max(1, height))
    return max(1, int(width * scale)), max(1, int(height * scale))


def _write_sheet(items: list[dict[str, Any]], output: Path, *, columns: int = 4,
                 tile_width: int = 320, tile_height: int = 220,
                 ffmpeg: str | None = None, fit: bool = False,
                 font_size: int | None = None) -> bool:
    """Create one labelled contact sheet for a vision call.

    ``fit`` keeps each frame's aspect ratio and pads the rest of the tile:
    stretching a 9:16 frame into a landscape tile made packaging text
    unreadable.
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        # Pillow is present in the Mac validation environment even when the
        # optional OpenCV wheel is absent.  Keep the sheet format identical so
        # Windows and Mac produce the same labelled visual input.
        try:
            from PIL import Image, ImageDraw, ImageFont  # type: ignore
        except Exception:
            # Windows production Python intentionally has no NumPy/Pillow
            # requirement.  JianYing bundles ffmpeg, so use its image2/tile
            # filters as a dependency-free fallback and draw a numeric label
            # that is mapped to shot_id in the prompt.
            command = ffmpeg or os.environ.get("JY_FFMPEG") or shutil.which("ffmpeg")
            if not command:
                return False
            tile_dir = output.parent / (output.stem + "_tiles")
            try:
                tile_dir.mkdir(parents=True, exist_ok=True)
                for index, item in enumerate(items, 1):
                    shutil.copyfile(item["path"], tile_dir / f"tile_{index:04d}.jpg")
                rows = max(1, (len(items) + columns - 1) // columns)
                if fit:
                    vf = (
                        f"scale={tile_width}:{tile_height - 34}:force_original_aspect_ratio=decrease,"
                        f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-34-ih)/2:color=0x191919,"
                        f"tile={columns}x{rows}"
                    )
                else:
                    vf = (
                        f"scale={tile_width}:{tile_height - 34},"
                        f"drawbox=y={tile_height - 34}:h=34:color=black@1:t=fill,"
                        f"tile={columns}x{rows}"
                    )
                proc = subprocess.run(
                    [str(command), "-y", "-hide_banner", "-loglevel", "error",
                     "-framerate", "1", "-i", str(tile_dir / "tile_%04d.jpg"),
                     "-vf", vf, "-frames:v", "1", str(output)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=120, check=False,
                )
                return proc.returncode == 0 and output.is_file() and output.stat().st_size > 0
            except (OSError, subprocess.SubprocessError, shutil.Error):
                return False
            finally:
                shutil.rmtree(tile_dir, ignore_errors=True)
        rows = max(1, (len(items) + columns - 1) // columns)
        canvas = Image.new("RGB", (columns * tile_width, rows * tile_height), (25, 25, 25))
        draw = ImageDraw.Draw(canvas)
        font = None
        if font_size:
            try:
                font = ImageFont.load_default(size=font_size)
            except Exception:
                font = None
        if font is None:
            try:
                font = ImageFont.load_default()
            except Exception:
                font = None
        for index, item in enumerate(items):
            row, col = divmod(index, columns)
            x, y = col * tile_width, row * tile_height
            try:
                image = Image.open(item["path"]).convert("RGB")
                if fit:
                    size = _fit_size(image.width, image.height, tile_width, tile_height - 34)
                    image = image.resize(size)
                    canvas.paste(image, (x + (tile_width - size[0]) // 2,
                                         y + (tile_height - 34 - size[1]) // 2))
                else:
                    canvas.paste(image.resize((tile_width, tile_height - 34)), (x, y))
            except Exception:
                draw.text((x + 8, y + 60), "FRAME_UNREADABLE", fill=(255, 0, 0), font=font)
            draw.rectangle((x, y + tile_height - 34, x + tile_width, y + tile_height), fill=(0, 0, 0))
            label_y = y + tile_height - 34 + max(2, (34 - font_size) // 2) if font_size else y + tile_height - 25
            draw.text((x + 5, label_y), str(item["label"])[:42],
                      fill=(255, 255, 255), font=font)
        output.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output, quality=90)
        return output.is_file() and output.stat().st_size > 0
    rows = max(1, (len(items) + columns - 1) // columns)
    canvas = np.zeros((rows * tile_height, columns * tile_width, 3), dtype=np.uint8)
    canvas[:] = (25, 25, 25)
    for index, item in enumerate(items):
        row, col = divmod(index, columns)
        x, y = col * tile_width, row * tile_height
        if fit:
            raw = cv2.imread(str(item["path"]))
            image = None
            if raw is not None:
                w, h = _fit_size(raw.shape[1], raw.shape[0], tile_width, tile_height - 34)
                image = cv2.resize(raw, (w, h), interpolation=cv2.INTER_AREA)
        else:
            image = _load_image(Path(item["path"]), tile_width, tile_height - 34)
        if image is None:
            cv2.putText(canvas, "FRAME_UNREADABLE", (x + 8, y + 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 1, cv2.LINE_AA)
        elif fit:
            h, w = image.shape[:2]
            ox, oy = x + (tile_width - w) // 2, y + (tile_height - 34 - h) // 2
            canvas[oy:oy + h, ox:ox + w] = image
        else:
            canvas[y:y + tile_height - 34, x:x + tile_width] = image
        label = str(item["label"])
        cv2.rectangle(canvas, (x, y + tile_height - 34),
                      (x + tile_width, y + tile_height), (0, 0, 0), -1)
        cv2.putText(canvas, label[:42], (x + 5, y + tile_height - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, font_size / 30 if font_size else 0.43,
                    (255, 255, 255), 2 if font_size else 1, cv2.LINE_AA)
    output.parent.mkdir(parents=True, exist_ok=True)
    return bool(cv2.imwrite(str(output), canvas))


# The loop needs a shell to manufacture new images to look at, so removing
# the shell removes the loop.  The sheet still reaches the model through
# --image, and view_image stays available for inspecting that one image.
_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "code_mode_host",
    "browser_use",
    "computer_use",
    "apps",
)

# The prompt already demands JSON-only output, but a prompt is a request and
# the model ignored it whenever it decided to investigate first.
# --output-schema would turn the shape into a constraint, but it is not
# universally available: the aijws relay answered 400 "<InvalidParameter:
# This response_format type is unavailable now>" and the batch never reached
# the model (measured 2026-09-16 against deepseek-v4.1-flash).  Not re-tested
# on official deepseek-flash, so the schema stays opt-in rather than default.  The
# parser in enrich_with_vision still accepts either the object or a bare
# array, so enabling it later is a one-variable change.
_VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "shot_id": {"type": "string"},
                    "description": {"type": "string"},
                    "visual_tags": {"type": "array", "items": {"type": "string"}},
                    "evidence_tags": {"type": "array", "items": {"type": "string"}},
                    "action_complete": {"type": "boolean"},
                    "head_waste": {"type": "number"},
                    "tail_waste": {"type": "number"},
                    "evidence_time": {"type": "number"},
                    "evidence_intervals": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"start": {"type": "number"}, "end": {"type": "number"}},
                        "required": ["start", "end"], "additionalProperties": False}},
                    "action_segments": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"start": {"type": "number"}, "end": {"type": "number"},
                                       "action": {"type": "string"},
                                       "result": {"type": ["string", "null"]}},
                        "required": ["start", "end", "action", "result"],
                        "additionalProperties": False}},
                    "text_tiles": {"type": "array", "items": {"type": "integer"}},
                    "readable_claims": {"type": "array", "items": {"type": "string"}},
                    "pointing_to_text": {"type": "boolean"},
                    "pointing_action": {"type": "string"},
                    "gesture": {"type": "string"},
                    "confidence": {"type": "number"},
                    # DEMO_ACTIONS V0.2 素材侧字段（见 demo_actions.py）。
                    # 前四项是匹配的判据，后四项是反重复与空镜判据。
                    "action": {"type": "string"},
                    "object": {"type": "string"},
                    "result": {"type": ["string", "null"]},
                    "role": {"type": "string", "enum": [
                        "direct_evidence", "usage_demo", "visual_metaphor",
                        "product_display", "context", "CTA"]},
                    "evidence_strength": {"type": "string", "enum": ["A", "B", "C"]},
                    "action_phase": {"type": "string",
                                     "enum": ["prep", "perform", "result", "static"]},
                    "setup_id": {"type": "string"},
                    "has_subject": {"type": "boolean"},
                },
                # 新增的 8 项**不在 required 里**：旧模型/旧中转可能不产出它们，
                # 写进 required 会让整批校验失败、所有镜头回落 pending_analysis。
                # 缺失时 demo_actions.judge() 走 ungated 分支放行，覆盖率另写入
                # claim_gate_coverage，不会静默假装闸门生效。
                "required": ["shot_id", "description", "visual_tags", "evidence_tags",
                             "action_complete", "head_waste", "tail_waste",
                             "evidence_time", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["shots"],
    "additionalProperties": False,
}


def _schema_enabled() -> bool:
    """Gate --output-schema; the relay rejects structured output by default."""
    return os.environ.get("JY_VISION_OUTPUT_SCHEMA", "").strip().lower() in (
        "1", "true", "yes", "on")


def _write_schema(directory: Path) -> Path | None:
    """Materialise the batch schema for --output-schema; never fatal."""
    path = directory / "vision_schema.json"
    try:
        path.write_text(json.dumps(_VISION_SCHEMA, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return None
    return path


def _vision_summary(profile: str, batches: list[Any], responses: list[dict[str, Any]],
                    errors: list[dict[str, Any]], *, partial: bool) -> dict[str, Any]:
    """Describe the batch run as it stands, including a partial one.

    The incremental persist writes this before each batch, so a file left
    behind by a failed run reports how many batches actually returned instead
    of carrying a stale count from an earlier attempt.
    """
    transport = _vision_transport()
    summary = {
        "provider": {"codex": "codex_exec_image", "anthropic": "anthropic_messages",
                     "volc_glm": "volc_chat"}.get(transport, "volc_chat"),
        "transport": transport,
        "profile": profile,
        "batch_count": len(batches),
        "responses": list(responses),
        "errors": list(errors),
        "partial": partial,
    }
    if transport == "volc_glm":
        summary["model"] = _volc_settings()["model"]
    return summary


_PROGRESS_LOCK = threading.Lock()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _persist(report_dir: Path, analysis: dict[str, Any]) -> None:
    """Write shot_candidates.json incrementally, after every batch.

    v1.3.20: this used to be the single write after the last batch, so one
    batch that hung or timed out discarded every already-validated row from
    the batches before it.  Persisting per batch makes the surviving work
    usable even when a later batch fails.  Readers (monitors, the next run)
    must never see a half-written file, hence the atomic replace.
    """
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(report_dir / "shot_candidates.json",
                      json.dumps(analysis, ensure_ascii=False, indent=2))
    except OSError:
        pass


def _progress(report_dir: Path, **fields: Any) -> None:
    """Where the analysis is right now, so a killed run says which step hung."""
    path = report_dir / "vision_progress.json"
    with _PROGRESS_LOCK:
        try:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            state = state if isinstance(state, dict) else {}
            state.update(fields)
            state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            state["pid"] = os.getpid()
            report_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2))
        except OSError:
            pass


def needs_vision(row: dict[str, Any]) -> bool:
    """动作没认证完，或包装文字的高清复核还没做完。CLI 与 enrich 共用这一个判据。"""
    return row.get("status") != "ready_for_matching" or row.get("text_pass") == "pending"


def _hold_text(row: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """排进高清文字复核：低清读到的字在复核完成前不算证据，复核要看的帧记在行上供续跑。"""
    row["text_pass"] = "pending"
    row["text_request_frames"] = [
        {"path": str(item["path"]), "timestamp": _number(item.get("timestamp")),
         "orientation": item.get("orientation") or "landscape"}
        for item in items if item.get("path")]
    row["readable_claims"] = []
    row["pointed_claims"] = []


def _resume_text_items(row: dict[str, Any]) -> list[dict[str, Any]]:
    """续跑文字复核要看的帧；旧文件没记就退回审核帧/抽样帧。帧文件都不在时返回空。"""
    raw = [f for f in row.get("text_request_frames") or [] if isinstance(f, dict)]
    if not raw:
        intervals = [i for i in row.get("evidence_intervals") or [] if isinstance(i, dict)]
        raw = [{"path": i.get("frame_path"), "timestamp": i.get("frame_time")}
               for i in intervals[:1] if i.get("frame_path")]
    if not raw:
        raw = [f for f in row.get("vision_frames") or [] if isinstance(f, dict)][:1]
    items = []
    for frame in raw[:2]:
        path = str(frame.get("path") or "")
        if not path or not Path(path).is_file():
            continue
        items.append({"path": path, "timestamp": _number(frame.get("timestamp")),
                      "orientation": frame.get("orientation") or _orientation(path),
                      "shot_id": str(row.get("shot_id") or "")})
    return items


def _vision_cache_path(report_dir: Path) -> Path:
    """Resolve the cross-task visual understanding cache.

    A task report remains the source of truth for its own result.  This cache is
    only a reusable accelerator, so it lives beside the queue work root and is
    invalidated by the source signature, shot window, profile, or analyzer
    version.  ``JY_VISION_CACHE_DIR`` lets the Windows worker place it on a
    persistent local/NAS cache without changing task manifests.
    """
    configured = str(os.environ.get("JY_VISION_CACHE_DIR") or "").strip()
    root = Path(configured) if configured else report_dir.parent.parent / ".cache"
    return root / "material_vision.json"


def _analysis_rules(manifest: dict[str, Any]) -> list[str]:
    try:
        from . import rulepack
        return list(rulepack.prompt_rules(manifest, "analysis"))
    except Exception:
        return []


def _keyed_rules(manifest: dict[str, Any]) -> list[str]:
    try:
        from . import rulepack
        return rulepack.keyed_analysis_rules(manifest)
    except Exception:
        return []


def _rule_checks(manifest: dict[str, Any]) -> dict[str, list[str]]:
    try:
        from . import rulepack
        pairs = rulepack.analysis_rechecks(manifest)
    except Exception:
        return {}
    return {hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]: words for text, words in pairs}


_RECHECK_FIELDS = ("description", "visual_description", "visual_tags", "evidence_tags",
                   "action", "object", "result")


def _needs_recheck(row: dict[str, Any] | None, checks: dict[str, list[str]]) -> bool:
    """旧观察没按某条新分析要求看过，且描述里沾着这条要求关心的词。"""
    done = set((row or {}).get("rule_checks") or [])
    words = [w for rid, ws in checks.items() if rid not in done for w in ws]
    if not words:
        return False
    parts = [str(v) for f in _RECHECK_FIELDS for v in _as_list((row or {}).get(f))]
    parts += [f"{s.get('action') or ''}{s.get('result') or ''}"
              for s in (row or {}).get("action_segments") or [] if isinstance(s, dict)]
    observed = "。".join(parts)
    return any(w in observed for w in words)


def _recheck_policy() -> str:
    """分析要求增量复核策略。

    - ``reuse``（默认）：缓存键一致且已有可用描述时，只盖章 ``rule_checks`` 复用，
      不再整段调视觉。卫品 ``recheck`` 词很宽时避免每升一次规则就重看整池。
    - ``vision``：命中 ``recheck`` 词就重新看图（旧行为）。
    - ``off``：忽略增量复核。
    """
    raw = os.environ.get("JY_VISION_RECHECK", "reuse").strip().lower()
    if raw in {"0", "off", "false", "no", "none"}:
        return "off"
    if raw in {"vision", "force", "1", "true", "yes", "full"}:
        return "vision"
    return "reuse"


def _stamp_rule_checks(row: dict[str, Any], checks: dict[str, list[str]]) -> dict[str, Any]:
    if not checks:
        return row
    return {**row, "rule_checks": sorted(checks)}


def _resolve_recheck(row: dict[str, Any] | None, checks: dict[str, list[str]]
                     ) -> tuple[dict[str, Any] | None, str]:
    """返回 ``(观察, 处置)``：``keep`` / ``stamp`` / ``redo``。"""
    if not isinstance(row, dict) or not _needs_recheck(row, checks):
        return row, "keep"
    policy = _recheck_policy()
    if policy == "off":
        return _stamp_rule_checks(row, checks), "stamp"
    ready = (row.get("status") == "ready_for_matching"
             and bool(str(row.get("description") or "").strip()))
    if policy == "reuse" and ready:
        return _stamp_rule_checks(row, checks), "stamp"
    return None, "redo"


def _vision_cache_key(manifest: dict[str, Any], shot: dict[str, Any], profile: str,
                      rules: list[str] | None = None) -> str:
    source = str(shot.get("video") or "").strip()
    if not source:
        return ""
    try:
        path = Path(source).expanduser().resolve()
        stat = path.stat()
        signature = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
        source = str(path)
    except OSError:
        return ""
    from . import frame_quality
    identity = {
        "version": VISION_ANALYSIS_VERSION,
        "profile": profile,
        "product": str(manifest.get("product_id") or manifest.get("product")
                        or manifest.get("brand") or "").strip(),
        "video": source,
        "signature": signature,
        "source_start_us": int(round(_number(shot.get("source_start")) * 1_000_000)),
        "source_end_us": int(round(_number(shot.get("source_end")) * 1_000_000)),
        "analysis_rules": hashlib.sha256(json.dumps(
            _keyed_rules(manifest) if rules is None else rules,
            ensure_ascii=False).encode("utf-8")).hexdigest()[:16],
        # Sampling grid + quality gate are part of the observation protocol.
        # Changing them must not reuse stale contact-sheet answers.
        "frame_protocol": {
            "step_s": SAMPLE_STEP_S,
            "sample_max": env_int("JY_VISION_SAMPLE_MAX", SAMPLE_MAX, minimum=2),
            "quality": frame_quality.FRAME_QUALITY_VERSION if frame_quality.enabled() else "off",
        },
    }
    transport = _vision_transport()
    if transport != "codex":
        # 非 Codex 缓存键带上传输与模型，避免不同后端观察互相复用。
        identity["transport"] = transport
        identity["model"] = os.environ.get("JY_VISION_MODEL", "").strip() or "glm-5.3-flash"
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_vision_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_vision_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    """并行任务共用这一份缓存：加锁合并写，不让后写的进程抹掉别人刚存的观察。"""
    def merge(disk: Any) -> dict[str, Any]:
        disk = dict(disk) if isinstance(disk, dict) else {}
        disk.update(cache)
        cache.update({k: v for k, v in disk.items() if k not in cache})
        return disk

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        locked_json_update(path, merge, default={}, timeout=120)
    except ValueError:
        try:
            _atomic_write(path, json.dumps(cache, ensure_ascii=False, indent=2))
        except OSError:
            pass
    except (OSError, TimeoutError):
        pass


def _text_pass_enabled() -> bool:
    return os.environ.get("JY_VISION_TEXT_PASS", "1").strip().lower() not in (
        "0", "false", "no", "off")


_TEXT_SPLIT = re.compile(r"[\s*·•^＊、，,;；|/]+")
_TEXT_NOISE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def _text_key(atom: str) -> str:
    return _TEXT_NOISE.sub("", atom).upper().replace("O", "0")


def agreed_text(reads: list[list[str]]) -> list[str]:
    """Phrases every independent read agrees on.

    Small packaging print comes back different on each read ("0致敏" read as
    "0酒精"), while text that is actually legible repeats; keeping only the
    agreement stops a misread from becoming package evidence.
    """
    if not reads:
        return []
    atoms = [[a for item in read for a in _TEXT_SPLIT.split(str(item)) if _text_key(a)]
             for read in reads]
    others = [{_text_key(a) for a in atoms[i]} | {_text_key(str(item)) for item in reads[i]}
              for i in range(1, len(reads))]
    kept: list[str] = []
    seen: set[str] = set()
    for atom in atoms[0]:
        key = _text_key(atom)
        if key in seen or not all(key in other for other in others):
            continue
        seen.add(key)
        kept.append(atom.strip())
    return kept


def apply_cross_shot_text(shots: list[dict[str, Any]]) -> None:
    """Package text counts as read only when other shots of the task read it too.

    The same pack shows up in many takes, so legible print repeats; a phrase
    found in one take only is treated as unread (a gap, never evidence).
    """
    minimum = env_int("JY_VISION_TEXT_MIN_SHOTS", 2, minimum=1)
    counts: dict[str, int] = {}
    for row in shots:
        for key in {_text_key(a) for a in row.get("text_agreed") or []}:
            counts[key] = counts.get(key, 0) + 1
    for row in shots:
        if "text_agreed" not in row:
            continue
        keep = {_text_key(a) for a in row["text_agreed"] if counts.get(_text_key(a), 0) >= minimum}
        row["readable_claims"] = [a for a in row["text_agreed"] if _text_key(a) in keep]
        row["pointed_claims"] = [a for a in row.get("pointed_agreed") or [] if _text_key(a) in keep]


def _cache_projection(row: dict[str, Any]) -> dict[str, Any] | None:
    """Keep only stable visual facts; never cache task-local frame paths/status noise."""
    if row.get("status") != "ready_for_matching" or row.get("action_complete") is not True:
        return None
    if row.get("text_pass") in ("pending", "failed"):
        return None
    projection = {field: row[field] for field in _VISION_CACHE_FIELDS if field in row}
    if isinstance(projection.get("evidence_intervals"), list):
        projection["evidence_intervals"] = [
            {k: v for k, v in item.items() if k != "frame_path"}
            for item in projection["evidence_intervals"] if isinstance(item, dict)]
    return projection


def _apply_cached_visual(row: dict[str, Any], cached: dict[str, Any], key: str) -> None:
    for field in _VISION_CACHE_FIELDS:
        if field in cached:
            row[field] = cached[field]
    row["analysis_cache_hit"] = True
    row["analysis_cache_key"] = key


def _annotate_shot_labels(manifest: dict[str, Any], shots: list[dict[str, Any]]) -> None:
    """品类规则把可见卖点补进标签（不读文件夹名）；缓存命中和新分析都走这里。"""
    try:
        from . import rulepack
        rules = rulepack.for_manifest(manifest)
    except Exception:
        return
    if not rules.get("shot_label_rules"):
        return
    for shot in shots:
        if not isinstance(shot, dict) or not shot.get("description"):
            continue
        rulepack.annotate_shot_labels(shot, rules)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a codex process tree, descendants before the cmd.exe wrapper.

    Killing the wrapper first reaps cmd.exe only; taskkill /T can then no
    longer enumerate the tree and the codex.exe/node.exe grandchildren leak as
    orphans.  Order matters.
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=False)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=30)
    except Exception:
        pass


def _run_codex_file(args: list[str] | str, prompt_path: Path, stdout_path: Path,
                    stderr_path: Path, timeout: int) -> tuple[bool, int, str]:
    """Run codex with file-backed stdio and a tree-killing timeout.

    Never pipe codex stdio.  On Windows codex runs through a cmd.exe wrapper;
    killing the cmd on timeout leaves the orphaned codex.exe children holding
    the inherited pipe write handles, and the post-kill drain then blocks
    forever waiting for EOF that never comes.  A timeout has to be a real
    process-tree kill, which only works when nothing is draining a pipe.

    Every codex invocation in this module goes through here so that the rule
    cannot be honoured in one call site and forgotten in another.
    """
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with open(stdout_path, "w", encoding="utf-8", errors="replace") as fout, \
         open(stderr_path, "w", encoding="utf-8", errors="replace") as ferr, \
         open(prompt_path, "rb") as fin:
        try:
            proc = subprocess.Popen(args, stdin=fin, stdout=fout, stderr=ferr)
        except OSError as exc:
            return False, -1, str(exc)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return False, -1, f"timed out after {timeout}s"
    return proc.returncode == 0, proc.returncode, ""


def _tail(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _clean_profile(value: Any) -> str:
    """Normalize a Windows-inherited Codex profile before CLI assembly.

    SSH/cmd wrappers can preserve one layer of surrounding quotes in an
    environment value. Passing that through makes Codex parse ``"flash`` as
    the profile name. Strip only whitespace and quote wrappers; never alter
    the profile's actual name.
    """
    return str(value or "").strip().strip('"').strip("'").strip()


def _codex_command(sheet: Path | None, response: Path, profile: str, *,
                   workdir: Path | None = None,
                   schema: Path | None = None) -> list[str] | str:
    profile = _clean_profile(profile)
    args = ["codex", "exec"]
    if profile:
        args.extend(["-p", profile])
    provider_override = os.environ.get("JY_VISION_PROVIDER", "").strip()
    if provider_override:
        args.extend(["-c", f"model_provider={provider_override}"])
    model_override = os.environ.get("JY_VISION_MODEL", "").strip()
    if model_override:
        args.extend(["-c", f"model={model_override}"])
    for feature in _DISABLED_FEATURES:
        args.extend(["--disable", feature])
    args.extend([
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check", "--ephemeral"])
    if sheet is not None:
        args.extend(["--image", str(sheet)])
    args.extend(["-o", str(response)])
    if workdir is not None:
        # Keep the agent's cwd inside the throwaway batch directory.  Without
        # this it inherits the task workdir and drops crop artefacts
        # (tmp_tile_*.png) into the production task folder.
        args.extend(["-C", str(workdir)])
    if schema is not None and _schema_enabled():
        # Single choke point: no caller can put --output-schema on the command
        # line while the relay still rejects structured output.
        args.extend(["--output-schema", str(schema)])
    args.append("-")
    if os.name == "nt":
        # npm installs Codex as a .cmd shim on Windows.  Going through cmd.exe
        # is the same rule used by run_task.template.ps1.  The command must be
        # a single string: a list makes Popen re-quote the inner command line
        # with backslash escapes that cmd.exe does not understand, which splits
        # every path containing a space into two broken arguments.  ``/s``
        # strips exactly the outer pair of quotes added here.
        import subprocess as _sp
        return f'cmd.exe /d /s /c "{_sp.list2cmdline(args)}"'
    return args


def _ccswitch_env() -> dict[str, str]:
    """Load the CC Switch CodingPlan transport without logging credentials."""
    values = {
        "base_url": os.environ.get("ANTHROPIC_BASE_URL", "").strip(),
        "auth_token": os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip(),
        "model": os.environ.get("JY_VISION_MODEL", "").strip()
        or os.environ.get("ANTHROPIC_MODEL", "").strip(),
    }
    env_file = os.environ.get("JY_VISION_ANTHROPIC_ENV", "").strip()
    if env_file and Path(env_file).is_file():
        try:
            raw = json.loads(Path(env_file).read_text(encoding="utf-8"))
            for key in values:
                if str(raw.get(key) or "").strip():
                    values[key] = str(raw[key]).strip()
        except (OSError, ValueError, TypeError):
            pass
    return values


def _run_anthropic_messages(sheet: Path, prompt: str, output: Path,
                            *, timeout: int) -> tuple[bool, str]:
    """Run a one-shot image request against the CC Switch Anthropic provider.

    CC Switch's Agent Plan endpoint speaks Anthropic Messages, while Codex's
    native runner speaks OpenAI Responses.  Keeping this adapter here lets the
    Windows worker use the selected CodingPlan quota without pretending the
    endpoint is a Codex Responses provider.
    """
    cfg = _ccswitch_env()
    if not cfg["base_url"] or not cfg["auth_token"] or not cfg["model"]:
        return False, "CCSWITCH_ANTHROPIC_CONFIG_MISSING"
    try:
        image = base64.b64encode(sheet.read_bytes()).decode("ascii")
    except OSError as exc:
        return False, f"CCSWITCH_IMAGE_READ_FAILED:{exc}"
    media_type = mimetypes.guess_type(sheet.name)[0] or "image/jpeg"
    body = {
        "model": cfg["model"],
        "max_tokens": int(os.environ.get("JY_VISION_MAX_TOKENS", "4096")),
        # Volcengine GLM accepts enabled thinking with a bounded budget. An
        # unbounded/default budget can consume all output tokens and leave an
        # empty text block, while `disabled` is rejected by this model.
        "thinking": {"type": "enabled", "budget_tokens": int(
            os.environ.get("JY_VISION_THINKING_BUDGET", "2048"))},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": image,
            }},
        ]}],
    }
    url = cfg["base_url"].rstrip("/") + "/v1/messages"
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": cfg["auth_token"],
        }, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, urllib.error.HTTPError, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            detail = exc.read(800).decode("utf-8", "replace")
            return False, f"CCSWITCH_HTTP_{exc.code}:{detail[-500:]}"
        return False, f"CCSWITCH_REQUEST_FAILED:{exc}"
    parts = [item.get("text", "") for item in payload.get("content", [])
             if isinstance(item, dict) and item.get("type") == "text"]
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        return False, "CCSWITCH_EMPTY_RESPONSE"
    output.write_text(text, encoding="utf-8")
    return True, text


def _run_vision(sheet: Path, prompt: str, output: Path, *, profile: str,
                timeout: int, workdir: Path | None = None,
                schema: Path | None = None) -> tuple[bool, str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = output.with_suffix(".stdout.log")
    stderr_path = output.with_suffix(".stderr.log")
    prompt_path = output.with_suffix(".prompt.txt")
    prompt_path.write_text(prompt, encoding="utf-8")
    transport = _vision_transport()
    if transport == "anthropic":
        return _run_anthropic_messages(sheet, prompt, output, timeout=timeout)
    if transport == "volc_glm":
        return _run_volc_chat(sheet, prompt, output, timeout=timeout)
    ok, code, error = _run_codex_file(
        _codex_command(sheet, output, profile, workdir=workdir, schema=schema),
        prompt_path, stdout_path, stderr_path, timeout)
    if error:
        if error.startswith("timed out"):
            return False, f"vision batch {error}"
        return False, error
    # --output-last-message is the canonical final response.  Some older CLI
    # builds do not create it when the provider fails, so keep the log files
    # as a diagnostic fallback without treating logs as model JSON.
    if output.is_file() and output.stat().st_size:
        return ok, output.read_text(encoding="utf-8", errors="replace")
    detail = _tail(stderr_path) or _tail(stdout_path) or f"codex exit {code}"
    if ok:
        return False, f"{EMPTY_RESPONSE}\n{detail}"
    return False, detail


EMPTY_RESPONSE = "vision model returned an empty response"


_ANTHROPIC_TRANSPORTS = {"anthropic", "ccswitch", "ccswitch_anthropic"}
_VOLC_TRANSPORTS = {"volc_glm", "glm", "volc"}


def _vision_transport() -> str:
    """Resolve transport; unset env uses Ark GLM-5.3-Flash direct HTTP."""
    raw = os.environ.get("JY_VISION_TRANSPORT", "").strip().lower()
    if raw in _ANTHROPIC_TRANSPORTS:
        return "anthropic"
    if raw in {"codex", "volc_codex"}:
        return "codex"
    if raw in _VOLC_TRANSPORTS or raw == "":
        return "volc_glm"
    return raw or "volc_glm"


def _direct_http_transport() -> bool:
    return _vision_transport() == "volc_glm"


def _contact_sheet_source() -> str:
    return {
        "codex": "codex_vision_contact_sheet_v3",
        "anthropic": "anthropic_vision_contact_sheet_v3",
        "volc_glm": "volc_glm_contact_sheet_v3",
    }.get(_vision_transport(), "volc_glm_contact_sheet_v3")


def _requested_batch_workers() -> int:
    """How many contact-sheet calls may be in flight.

    Codex stays at one agent process. Direct GLM HTTP defaults to 20.
    """
    raw = os.environ.get("JY_VISION_BATCH_WORKERS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return 1
    return 20 if _direct_http_transport() else 1


def _batch_workers(batch_count: int) -> int:
    return max(1, min(_requested_batch_workers(), max(1, batch_count)))


def _iter_indexed(jobs: list[Any], fn, workers: int):
    """Yield completed jobs so the caller can validate and checkpoint immediately."""
    if workers <= 1 or len(jobs) <= 1:
        for job in jobs:
            yield fn(job)
        return
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        futures = [pool.submit(fn, job) for job in jobs]
        for future in as_completed(futures):
            yield future.result()


def _call_indexed(jobs: list[Any], fn, workers: int) -> dict[int, Any]:
    return dict(_iter_indexed(jobs, fn, workers))


def _prepare_indexed(jobs: list[Any], fn, *, on_done=None) -> dict[int, Any]:
    """Finish media preparation before starting the independent API pool.

    Preparation slots cover CPU/image work across tasks as well as decoding;
    they are never held while waiting for the vision provider.
    """
    def prepare(job):
        with slot("vision_prepare", env_int("JY_VISION_PREP_SLOTS", 2, minimum=1),
                  timeout=env_float("JY_VISION_PREP_TIMEOUT", 600.0)) as handle:
            if not handle.acquired:
                raise TimeoutError("VISION_PREP_SLOT_TIMEOUT")
            return fn(job)

    found = {}
    workers = env_int("JY_VISION_PREP_WORKERS", 2, minimum=1)
    for index, value in _iter_indexed(jobs, prepare, workers):
        found[index] = value
        if on_done is not None:
            on_done(len(found), len(jobs))
    return found


def _redact_secret(text: str) -> str:
    text = re.sub(r"ark-[A-Za-z0-9_\-]{8,}", "ark-***", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]+", r"\1***", text)
    return text


def _volc_settings() -> dict[str, str]:
    return {
        "api_key": (os.environ.get("ARK_API_KEY", "").strip()
                    or os.environ.get("JY_VOLC_API_KEY", "").strip()),
        "base_url": (os.environ.get("JY_VOLC_BASE_URL", "").strip()
                     or "https://ark.cn-beijing.volces.com/api/plan/v3").rstrip("/"),
        "model": os.environ.get("JY_VISION_MODEL", "").strip() or "glm-5.3-flash",
    }


def _run_volc_chat(sheet: Path | None, prompt: str, output: Path,
                   *, timeout: int) -> tuple[bool, str]:
    """Post one contact sheet to Volcengine Ark chat completions.

    Skips Codex. ``glm-5.3-flash`` rejects ``thinking.type=disabled`` and
    spends the completion budget on reasoning when the cap is small, so thinking
    stays enabled with a bounded budget and a larger ``max_tokens``.
    """
    cfg = _volc_settings()
    if not cfg["api_key"]:
        return False, "VOLC_API_KEY_MISSING"
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if sheet is not None:
        try:
            image = base64.b64encode(sheet.read_bytes()).decode("ascii")
        except OSError as exc:
            return False, f"VOLC_IMAGE_READ_FAILED:{exc}"
        media_type = mimetypes.guess_type(sheet.name)[0] or "image/jpeg"
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{media_type};base64,{image}",
        }})
    if os.environ.get("JY_VISION_THINKING", "").strip().lower() == "disabled":
        thinking = {"type": "disabled"}
    else:
        thinking = {"type": "enabled", "budget_tokens": int(
            os.environ.get("JY_VISION_THINKING_BUDGET", "2048"))}
    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": content}],
        "thinking": thinking,
        "max_tokens": int(os.environ.get("JY_VISION_MAX_TOKENS", "8192")),
        "temperature": 0,
        "stream": False,
    }
    request = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {cfg['api_key']}",
        }, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = _redact_secret(exc.read(800).decode("utf-8", "replace"))
        return False, f"VOLC_HTTP_{exc.code}:{detail[-500:]}"
    except (OSError, ValueError) as exc:
        return False, f"VOLC_REQUEST_FAILED:{_redact_secret(str(exc))}"
    choices = payload.get("choices") or []
    message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
    out = str((message or {}).get("content") or "").strip()
    if not out:
        return False, EMPTY_RESPONSE
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(out, encoding="utf-8")
    except OSError:
        pass
    return True, out


_TRANSIENT_MARKERS = (
    "502", "503", "429", "too many requests", "temporarily unavailable",
    "timed out", "timeout", "connection reset", "connection refused", "network",
    "empty response", "empty_response",
)


def is_empty_failure(text: str) -> bool:
    lowered = str(text or "").casefold()
    return is_blank(text) or "empty response" in lowered or "empty_response" in lowered


def _is_transient(text: str) -> bool:
    lowered = str(text or "").casefold()
    if (re.search(r"http_40[13]\b", lowered)
            or "volc_api_key_missing" in lowered
            or "user location is not supported" in lowered):
        return False
    return is_blank(text) or any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _backoff_seconds(attempt: int) -> float:
    base = env_float("JY_VISION_BACKOFF", 3.0)
    delay = base * (2 ** attempt) * random.uniform(0.75, 1.25)
    return min(60.0, delay)


def _vision_slot():
    if os.environ.get("JY_VISION_SLOTS", "").strip():
        capacity = env_int("JY_VISION_SLOTS", 20)
    elif _direct_http_transport():
        capacity = max(20, _requested_batch_workers())
    else:
        capacity = 2
    return slot("vision", capacity,
                timeout=env_float("JY_VISION_SLOT_TIMEOUT", 600.0))


def _run_vision_with_retry(sheet: Path, prompt: str, output: Path, *,
                           profile: str, timeout: int,
                           workdir: Path | None = None,
                           schema: Path | None = None,
                           max_retries: int | None = None,
                           require_slot: bool = False,
                           on_attempt: Any = None) -> tuple[bool, str]:
    """Retry transient provider failures with jittered exponential backoff.

    Parallel runs hit 429/502/503, empty model output and channel timeouts
    from the relay; one failed batch used to hard-block the whole task.
    Attempts default to ``JY_VISION_RETRIES`` (4); ``max_retries`` overrides
    it for callers with a tight budget.  Deterministic failures return at
    once.  Every attempt holds a cross-process ``vision`` slot
    (``JY_VISION_SLOTS``); a slot wait timeout degrades to an unthrottled
    call instead of failing.  After exhausting retries the last
    ``(False, message)`` is returned unchanged.
    """
    attempts = (env_int("JY_VISION_RETRIES", 4, minimum=1) if max_retries is None
                else max(1, int(max_retries) + 1))
    last: tuple[bool, str] = (False, "")
    for attempt in range(attempts):
        if on_attempt is not None:
            on_attempt(attempt + 1, attempts, last[1][-300:] if attempt else "")
        with _vision_slot() as handle:
            if not handle.acquired:
                if require_slot:
                    return False, "VISION_SLOT_TIMEOUT"
                print(f"[vision] slot wait {handle.waited:.0f}s timed out; calling without throttle",
                      file=sys.stderr, flush=True)
            ok, text = _run_vision(sheet, prompt, output, profile=profile, timeout=timeout,
                                   workdir=workdir, schema=schema)
        if ok and not is_blank(text):
            return ok, text
        last = (False, EMPTY_RESPONSE) if ok else (ok, text)
        if attempt >= attempts - 1 or not _is_transient(last[1]):
            break
        time.sleep(_backoff_seconds(attempt))
    return last


def _shot_frames(shot: dict[str, Any]) -> list[dict[str, Any]]:
    paths = shot.get("frame_paths") or [shot.get("frame_path")]
    result = []
    for index, raw in enumerate(paths):
        if not raw:
            continue
        path = Path(str(raw))
        if not path.is_file():
            continue
        start = _number(shot.get("source_start"))
        end = _number(shot.get("source_end"), start)
        if index == 0:
            timestamp = (start + end) / 2.0
            suffix = "mid"
        else:
            timestamp = max(start, end - 0.15)
            suffix = "end"
        result.append({"path": str(path), "timestamp": timestamp, "suffix": suffix})
    return result


def sample_times(start: float, end: float, *, step: float = SAMPLE_STEP_S,
                 limit: int | None = None) -> list[float]:
    """Evenly spaced frame times for one shot, plus a frame just before the cut."""
    if limit is None:
        limit = env_int("JY_VISION_SAMPLE_MAX", SAMPLE_MAX, minimum=2)
    duration = max(0.0, end - start)
    if duration <= 0:
        return [round(start, 3)]
    count = max(2, min(limit, math.ceil(duration / step)))
    times = [start + duration * (i + 0.5) / count for i in range(count)]
    tail = max(start, end - 0.15)
    if tail - times[-1] > 0.3:
        times.append(tail)
    return [round(t, 3) for t in times]


def _sample_frames(shot: dict[str, Any], frame_dir: Path,
                   ffmpeg: str | None) -> list[dict[str, Any]]:
    video = Path(str(shot.get("video") or ""))
    start = _number(shot.get("source_start"))
    end = _number(shot.get("source_end"), start)
    if not str(shot.get("video") or "").strip() or not video.is_file() or end <= start:
        return _shot_frames(shot)
    from .shot_analyzer import extract_frame, extract_frames_batch
    from . import frame_quality
    times = sample_times(start, end)
    # Bucket half-width: distance from a sample to the midpoint between neighbors.
    half_width = (end - start) / (2.0 * max(1, len(times)))
    jobs = []
    reuse: list[dict[str, Any] | None] = []
    for index, timestamp in enumerate(times, 1):
        path = frame_dir / f"{shot['shot_id']}_{int(round(timestamp * 1000)):07d}.jpg"
        if path.is_file() and path.stat().st_size > 0 and not frame_quality.enabled():
            reuse.append({"path": str(path), "timestamp": timestamp, "suffix": f"t{index}"})
            continue
        reuse.append(None)
        jobs.append({
            "center": timestamp, "output": path, "start": start, "end": end,
            "half_width": half_width, "suffix": f"t{index}",
        })
    picked_list = frame_quality.select_frames(
        video, [{k: v for k, v in job.items() if k != "suffix"} for job in jobs],
        extract=extract_frame, extract_batch=extract_frames_batch, ffmpeg=ffmpeg,
    ) if jobs else []
    frames: list[dict[str, Any]] = []
    job_i = 0
    for index, cached in enumerate(reuse, 1):
        if cached is not None:
            frames.append(cached)
            continue
        picked = picked_list[job_i]
        job_i += 1
        if not picked.get("ok"):
            continue
        frames.append({
            "path": str(picked["path"]),
            "timestamp": float(picked["timestamp"]),
            "suffix": f"t{index}",
            "quality": picked.get("quality") or {},
            "replaced": bool(picked.get("replaced")),
        })
    if len(frames) >= 2:
        return frames
    return _shot_frames(shot) or frames


def _orientation(path: str) -> str:
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as image:
            return "portrait" if image.height > image.width else "landscape"
    except Exception:
        pass
    try:
        import cv2  # type: ignore
        image = cv2.imread(str(path))
        if image is not None:
            return "portrait" if image.shape[0] > image.shape[1] else "landscape"
    except Exception:
        pass
    return "landscape"


def _pack_batches(groups: list[list[dict[str, Any]]], limit: int) -> list[list[dict[str, Any]]]:
    """Pack whole shots into sheets of at most ``limit`` tiles, one orientation per sheet."""
    batches: list[list[dict[str, Any]]] = []
    for orientation in ("portrait", "landscape"):
        current: list[dict[str, Any]] = []
        for frames in groups:
            if not frames or frames[0].get("orientation") != orientation:
                continue
            if current and len(current) + len(frames) > limit:
                batches.append(current)
                current = []
            current.extend(frames)
        if current:
            batches.append(current)
    return batches


def _sheet_geometry(batch: list[dict[str, Any]], *, text: bool = False) -> dict[str, int]:
    portrait = (batch[0].get("orientation") if batch else "landscape") == "portrait"
    if text:
        width, height = _TEXT_PORTRAIT_TILE if portrait else _TEXT_LANDSCAPE_TILE
        columns = 2 if portrait else 1
    else:
        width, height = _PORTRAIT_TILE if portrait else _LANDSCAPE_TILE
        columns = 4 if portrait else 3
    return {"columns": min(columns, max(1, len(batch))), "tile_width": width,
            "tile_height": height + _LABEL_H}


def _label_number(value: Any) -> int | None:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else None


def _vision_prompt(items: list[dict[str, Any]], batch_index: int, batch_count: int,
                   rules: Iterable[str] = ()) -> str:
    mapping = "\n".join(
        f"- #{index}: shot_id={item['shot_id']}，素材时间 {item['timestamp']:.2f}s"
        f"（该镜头 {item['source_start']:.2f}-{item['source_end']:.2f}s）"
        for index, item in enumerate(items, 1)
    )
    rule_lines = [str(rule).strip() for rule in rules if str(rule).strip()]
    rule_text = ("\n分析规则（必须遵守）：\n" + "\n".join(f"- {r}" for r in rule_lines) + "\n"
                 if rule_lines else "")
    return f"""你是当前商品素材的视觉分析器。现在处理第 {batch_index}/{batch_count} 个抽帧批次。
图片是带编号的 contact sheet，从左到右、从上到下依次是 #1、#2……；每格左下角标着编号和该帧在原素材里的时间。同一个 shot_id 的几帧按时间先后排列，要把它们连起来看成一段视频。只分析图片中确实看见的内容，不能读取或猜测脚本文案，不能把图片里没有的卖点写进 evidence_tags。

各格对应关系：
{mapping}
{rule_text}
直接依据给出的 contact sheet 作答：不要裁剪、放大、重新查看图片，不要使用任何工具，也不要把中间过程写进输出。
    对每个 shot_id 输出一条 JSON。严格只输出 JSON 对象 {{"shots": [...]}}，不要 Markdown，不要额外字段。每条字段必须是：
    shot_id, description, visual_tags, evidence_tags, action_complete, head_waste, tail_waste, evidence_time, evidence_intervals,
    action_segments, text_tiles, confidence,
    action, object, result, role, evidence_strength, action_phase, setup_id, has_subject,
    readable_claims, pointing_to_text, pointing_action, gesture。
- description：按时间顺序客观描述整段画面；镜头里先后有几个动作就都写出来（例如“先把包装挂到挂钩上，再单手抽出一张”），不能只描述某一帧。
- action_segments：按时间顺序列出镜头里每个看得见的动作段，每项 {{"start": 秒, "end": 秒, "action": 动词短语, "result": 看得见的结果或 null}}；时间用格子上标的素材时间。静态陈列给一项 action 为“无动作”的段。
- visual_tags：主体、动作、场景、物体等可见标签。
- evidence_tags：只写从画面直接观察到、能用于后续口播匹配的事实，例如“纸张接触水面”“手部擦拭锅面”“完整展示包装”；不能写“可能”“应该”“看不清”。
- action：画面里主体正在做的动作，必须是**动词短语**（如“倾倒”“按压”“拉伸”“抚摸”“拉开”“撕开再粘贴”“堆叠”“举起”“无动作”）。参考片的匹配单位是动词，不是名词，这一项是整个匹配的关键。
- object：这个动作作用在什么上（如“蓝色液体+巾体”“已吸水巾体”“裤腰”“粘扣”“真棉花球+巾体”）。必须与 action 对应，不能写“产品”这种笼统词。
- result：这个动作产生了什么**在画面里直接看得见**的结果（如“液体被吸入”“按压处无水渗出”“可见弹性形变”“立体护边立起”）。这是证据强度的唯一依据：
  * 有可见结果 → 填结果本身；
  * 动作看得见但结果看不见（例如抚摸面层、拿产品蹭手背、往身上比试——这类只有动作，柔软/不适/合身都是触觉或判断，画面里看不到）→ 必须填 null，不得编造结果。
- role：从这六个里选一个：direct_evidence（有可见结果的证明性动作）、usage_demo（演示用法/形态，结果不可见）、visual_metaphor（道具或装置示意，如真棉花球、玻璃杯）、product_display（陈列产品，无卖点证明）、context（量感/场景/促销）、CTA（引导语镜头）。
- evidence_strength：A / B / C 三选一，只按 result 判：
  * A = result 非 null 且结果在画面内直接可见（倾倒后液面下降、按压后接触面无水、拉伸后可见形变）；
  * B = 动作演示了用法或属性但结果需要推断（拆包取出、产品入包、举着包装对镜头）；
  * C = 仅陈列，或只有动作而结果不可见（抚摸面层、蹭手背、往身上比试），或用道具/装置示意。
  **result 为 null 时不得填 A。**
- action_phase：prep（准备/掏出/撕开）、perform（正在执行该动作）、result（展示动作结果状态）、static（静态陈列，无进行中的动作），四选一。只有动作确实在进行时才填 perform；动作还没开始或已经结束、画面只是静物时填 static 或 result。
- setup_id：机位+构图标识。**同一台机器、同一角度、主体在画面中的位置与占比基本相同**的镜头必须填同一个 setup_id（自拟稳定短码，如“wood_table_front”“handheld_closeup_left”）。换景别、换角度、换主体位置就要换 setup_id。这一项用于防止同机位同构图连续重复。
    - has_subject：画面里是否存在本商品主体（true/false）。**整段都是背景、看不到产品本体时填 false**（空镜）。判断主体是否存在要看整段，不能只看某一帧；白色产品也算主体，不要因为颜色浅就判 false。
    - readable_claims：只有在包装文字/标识实际可读时逐字记录；看不清就填空数组。
    - text_tiles：包装正面或侧面文字正对镜头、最适合放大读字的格子编号（最多 2 个，如 [3, 5]）；画面里看不到包装文字就填空数组。之后会用这些格子的高清原图单独读字。
    - pointing_to_text：只有手指/手势明确指向该可读包装文字或标识才为 true；普通拿包装、展示包装、手在旁边不算指向。
    - pointing_action/gesture：简短描述指向动作；没有明确指向时填空。包装文字只可支持脚本中同一条包装卖点，不能证明真实性或吸收、柔软等功能。
    - 对 CTA、促销、划算、囤货、数量话术，只有多包、成排、整箱、堆叠或清晰数量关系才填写对应量感标签；单包静态特写不得当作 CTA 量感证据。
- action_complete：判断这个镜头里有没有一段可以直接用作口播证据的画面。判据是主体状态，不是画面里有没有人、有没有手：
  * 有人手拿着、举着、托着、指着、按着产品做展示——属于展示，只要产品主体完整可见就为 true；手入镜本身不构成 false 的理由。
  * 画面里没有人物，产品静置摆放（平铺、叠放、散落）——为 true。
  * 演示动作（抽取、拉伸、擦拭、冲淋、挂起等）只要镜头里有一段从动作开始到结果看得见的完整过程，就为 true，并把这段写进 action_segments；镜头开头或结尾多出来的半截动作不影响判断。
  * 只有整段都看不到一个完整动作或稳定展示、或产品主体始终被遮挡/未完整入镜时才为 false。
  * 不确定时按“产品主体是否完整可见、有没有一段完整动作”判断，不要因为有人手出现就判 false。
- head_waste/tail_waste：镜头开头/结尾属于无效画面的比例，0 到 1。无效指等待、空镜、动作尚未开始或已结束后的余量、机位调整、遮挡，也包括：手刚伸进画面或正在离开画面、停止录制前的定格或去拿手机的动作、调整机位/对焦/曝光造成的晃动（例：整段都是有效展示填 0.0；开头约三成在等动作开始填 0.3；最后一帧里手正在撤出画面时 tail_waste 至少填 0.2）。只填确实观察到的废料比例，确认没有废料才填 0.0，不要用 0.0 表示“不确定”。
- evidence_time：最能证明 evidence_tags 的那一帧的素材时间（秒，取格子上标的时间）。
- evidence_intervals：最能证明 evidence_tags 的一段画面，只给一项 [{{"start": 秒, "end": 秒}}]，长度不超过 {EVIDENCE_MAX_S:g} 秒，必须落在该镜头时间范围内。
- confidence：0 到 1。任何不确定都降低置信度并减少 evidence_tags。

{mapping}
"""


def _text_prompt(shot_id: str, items: list[dict[str, Any]]) -> str:
    frames = "、".join(f"#{index} 是素材 {item['timestamp']:.2f}s 的画面"
                      for index, item in enumerate(items, 1))
    return f"""你只做一件事：逐字抄录图中产品包装上清晰可读的文字。图里是同一个镜头 shot_id={shot_id} 的高清画面（{frames}）。
- 抄录品名、卖点短语、规格数字、图标旁边的文字，每条一个短语，保持原文用字，不翻译、不改写。
- 只写确实看清的字；看不清、被遮挡或反光的部分不要猜，也不要用字形相近的词替代。
- 手指或笔明确指着某条文字时，把那条文字写进 pointed_text；没有明确指向就填空数组。
不要裁剪、放大或使用任何工具，严格只输出 JSON 对象：{{"shot_id": "{shot_id}", "readable_claims": ["..."], "pointed_text": ["..."]}}
"""


def _action_segments(raw: Any, shot: dict[str, Any]) -> list[dict[str, Any]]:
    start = _number(shot.get("source_start"))
    end = max(start, _number(shot.get("source_end"), start))
    segments: list[dict[str, Any]] = []
    for value in raw if isinstance(raw, list) else []:
        if not isinstance(value, dict):
            continue
        left = max(start, _number(value.get("start"), start))
        right = min(end, _number(value.get("end"), end))
        action = str(value.get("action") or "").strip()
        if right <= left or not action:
            continue
        result = value.get("result")
        result = str(result).strip() if result is not None else ""
        segments.append({"start": round(left, 3), "end": round(right, 3),
                         "action": action, "result": result or None})
    return segments[:SAMPLE_MAX * 2]


_UNFINISHED_WORDS = ("进行中", "未完成", "没完成", "尚未", "还没", "中途", "未结束")


def segment_completed(segment: dict[str, Any] | None) -> bool:
    """模型给这段动作写了看得见的结果，且没说它没做完。"""
    if not isinstance(segment, dict):
        return False
    result = str(segment.get("result") or "").strip()
    text = f"{segment.get('action') or ''}{result}"
    return bool(result) and not any(word in text for word in _UNFINISHED_WORDS)


def _certified_segment(row: dict[str, Any]) -> dict[str, Any] | None:
    """审核帧落在其中、且模型认证做完了的那段动作。"""
    intervals = [i for i in row.get("evidence_intervals") or [] if isinstance(i, dict)]
    if not intervals:
        return None
    frame_time = _number(intervals[0].get("frame_time"),
                         (_number(intervals[0].get("start")) + _number(intervals[0].get("end"))) / 2.0)
    for segment in row.get("action_segments") or []:
        if (segment_completed(segment)
                and _number(segment.get("start")) <= frame_time <= _number(segment.get("end"))):
            return segment
    return None


def _nearest_frame(frames: list[dict[str, Any]], timestamp: float) -> dict[str, Any] | None:
    return min(frames, key=lambda f: abs(_number(f.get("timestamp")) - timestamp)) if frames else None


def _primary_interval(raw: dict[str, Any], row: dict[str, Any],
                      frames: list[dict[str, Any]], frame: dict[str, Any] | None,
                      *, evidence: bool) -> tuple[list[dict[str, Any]], bool]:
    """One evidence window of at most ``EVIDENCE_MAX_S``; True when the model supplied it.

    Downstream cuts must cover the whole interval, so a long or multi-part
    window would reject a shot that holds the action.
    """
    model = _evidence_intervals(raw.get("evidence_intervals"), row, None, evidence=evidence)
    if not model:
        return _evidence_intervals(None, row, frame, evidence=evidence), False
    left, right = model[0]["start"], model[0]["end"]
    anchor = _number(raw.get("evidence_time"), (left + right) / 2.0)
    if not left <= anchor <= right:
        anchor = (left + right) / 2.0
    if right - left > EVIDENCE_MAX_S:
        half = EVIDENCE_MAX_S / 2.0
        center = min(max(anchor, left + half), right - half)
        left, right = center - half, center + half
    inside = [f for f in frames if left - 1e-6 <= _number(f.get("timestamp")) <= right + 1e-6]
    chosen = _nearest_frame(inside, anchor) or _nearest_frame(frames, anchor)
    record: dict[str, Any] = {"start": round(left, 3), "end": round(right, 3)}
    if chosen:
        record["frame_path"] = chosen["path"]
        record["frame_time"] = round(_number(chosen.get("timestamp")), 3)
    return [record], True


def enrich_with_vision(manifest: dict[str, Any], analysis: dict[str, Any], report_dir: Path,
                      *, profile: str | None = None, batch_size: int = 12,
                      timeout: int | None = None, ffmpeg: str | None = None) -> dict[str, Any]:
    """Use the configured image backend to fill and validate temporary shots."""
    if timeout is None:
        timeout = env_int("JY_VISION_TIMEOUT", 600, minimum=60)
    profile = _clean_profile(
        profile if profile is not None else os.environ.get("JY_VISION_PROFILE", "volc"))
    shots = [row for row in analysis.get("shots", []) if isinstance(row, dict)]
    if not shots:
        return analysis
    stage_seconds: dict[str, float] = {}
    cache_path = _vision_cache_path(report_dir)
    cache = _load_vision_cache(cache_path)
    cache_hits = 0
    cache_misses = 0
    cache_keys: dict[str, str] = {}
    uncached_shots: list[dict[str, Any]] = []
    text_resume: dict[str, list[dict[str, Any]]] = {}
    seeded = 0
    stale = 0
    rechecked = 0
    recheck_reused = 0
    checks = _rule_checks(manifest)
    every_rule = _analysis_rules(manifest)
    for shot in shots:
        shot_id = str(shot.get("shot_id") or "")
        key = _vision_cache_key(manifest, shot, profile)
        if key:
            cache_keys[shot_id] = key
        cached = cache.get(key) if key else None
        if key and checks and set((cached or {}).get("rule_checks") or []) != set(checks):
            # 分析要求曾经整份进缓存键时，按全部要求看过的观察存在另一个键下。
            full = _vision_cache_key(manifest, shot, profile, every_rule)
            if (full != key and isinstance(cache.get(full), dict)
                    and cache[full].get("status") == "ready_for_matching"):
                cached = cache[key] = {**cache[full], "rule_checks": sorted(checks)}
                seeded += 1
        cached, cached_recheck = _resolve_recheck(cached, checks)
        if cached_recheck == "redo":
            rechecked += 1
        elif cached_recheck == "stamp":
            recheck_reused += 1
            if key and isinstance(cached, dict):
                cache[key] = cached
        if (isinstance(cached, dict)
                and cached.get("status") == "ready_for_matching"
                and shot.get("frame_exists") is not False):
            _apply_cached_visual(shot, cached, key)
            if (shot.get("action_complete_source") == "evidence_interval"
                    and not _certified_segment(shot)):
                shot.update({"action_complete": False, "status": "pending_analysis",
                             "action_complete_source": "model",
                             "analysis_error": "action_complete=false：模型判定该镜头动作未完成，"
                                               "定位到的动作段没有看得见的结果，未进入匹配池"})
            else:
                try:
                    from . import rulepack as _rp
                    _rp.annotate_shot_labels(shot, _rp.for_manifest(manifest))
                except Exception:
                    pass
            cache_hits += 1
            continue
        # 本任务候选文件里做完的观察：只有产生它的输入签名（商品、素材、窗口、
        # 规则、分析版本）与当前一致才保留并回种全局缓存；对不上就整条重做。
        local = (shot.get("status") == "ready_for_matching"
                 and bool(shot.get("description"))
                 and shot.get("frame_exists") is not False)
        _, local_recheck = _resolve_recheck(shot if local else None, checks)
        if local and key and shot.get("analysis_cache_key") == key and local_recheck != "redo":
            if local_recheck == "stamp":
                recheck_reused += 1
                shot["rule_checks"] = sorted(checks)
            if shot.get("text_pass") != "pending":
                shot["analysis_cache_hit"] = True
                projection = _cache_projection(shot)
                if projection is not None and (key not in cache or local_recheck == "stamp"):
                    cache[key] = projection
                    seeded += 1
                cache_hits += 1
                continue
            items = _resume_text_items(shot)
            if items:
                # 动作已认证，只补没做完的文字复核。
                shot["analysis_cache_hit"] = True
                _hold_text(shot, items)
                text_resume[shot_id] = items
                cache_hits += 1
                continue
        if local and local_recheck == "redo":
            rechecked += 1
        if local:
            stale += 1
        if shot.get("status") == "ready_for_matching":
            shot.update({"status": "pending_analysis", "readable_claims": [],
                         "pointed_claims": [],
                         "analysis_error": "旧观察与当前输入签名不一致或复核帧缺失，重新分析"})
            shot.pop("text_pass", None)
        shot["analysis_cache_hit"] = False
        if key:
            shot["analysis_cache_key"] = key
        cache_misses += 1
        uncached_shots.append(shot)
    shots_to_analyze = uncached_shots
    if seeded:
        try:
            _save_vision_cache(cache_path, cache)
        except (OSError, TimeoutError, ValueError):
            pass

    def _set_cache_summary(summary: dict[str, Any], *, partial: bool = False) -> None:
        summary.update({
            "analysis_version": VISION_ANALYSIS_VERSION,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "stale_local": stale,
            "rule_rechecked": rechecked,
            "rule_recheck_reused": recheck_reused,
            "text_resumed": len(text_resume),
            "cache_path": str(cache_path),
            "partial": partial,
            "stage_seconds": dict(stage_seconds),
            "source_sha256": _RUNTIME_SOURCE_SHA256,
        })
        analysis["vision_analysis"] = summary

    if not shots_to_analyze and not text_resume:
        _annotate_shot_labels(manifest, shots)
        apply_cross_shot_text(shots)
        _set_cache_summary(_vision_summary(profile, [], [], [], partial=False))
        analysis["pending_items"] = list(analysis.get("pending_items", []))
        analysis["ok"] = not any(needs_vision(row) for row in shots)
        _persist(report_dir, analysis)
        _progress(report_dir, phase="done", vision_left=0, text_left=0)
        return analysis
    report_dir.mkdir(parents=True, exist_ok=True)
    frame_dir = report_dir / "vision_frames"
    groups: list[list[dict[str, Any]]] = []
    shot_frames: dict[str, list[dict[str, Any]]] = {}
    _progress(report_dir, phase="frame_prepare", prepare_done=0,
              prepare_total=len(shots_to_analyze), batch_workers=0,
              requests_finished=0, finished_at=None)
    stage_started = time.monotonic()

    def _prepare_frames(job):
        index, shot = job
        return index, _sample_frames(shot, frame_dir, ffmpeg)

    sampled = _prepare_indexed(list(enumerate(shots_to_analyze)), _prepare_frames,
        on_done=lambda done, total: _progress(report_dir, prepare_done=done, prepare_total=total))
    stage_seconds["frame_prepare"] = round(time.monotonic() - stage_started, 3)
    for index, shot in enumerate(shots_to_analyze):
        frames = sampled[index]
        if not frames:
            shot.setdefault("status", "pending_analysis")
            shot.setdefault("analysis_error", "no readable frame")
            continue
        orientation = _orientation(frames[0]["path"])
        group = [{**frame, "shot_id": shot["shot_id"], "orientation": orientation,
                  "source_start": _number(shot.get("source_start")),
                  "source_end": _number(shot.get("source_end"))} for frame in frames]
        shot_frames[str(shot["shot_id"])] = group
        groups.append(group)
    tile_limit = env_int("JY_VISION_SHEET_TILES", SHEET_TILES, minimum=2)
    # A shot is never split across sheets, so one response sees its whole timeline.
    batches = _pack_batches(groups, max(tile_limit, max((len(g) for g in groups), default=0)))
    analysis_rules = _analysis_rules(manifest)
    by_id = {str(row.get("shot_id")): row for row in shots}
    vision_errors: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    text_requests: dict[str, list[dict[str, Any]]] = dict(text_resume)
    text_errors: list[dict[str, Any]] = []

    def _remember(shot_id: str) -> None:
        cache_key = cache_keys.get(shot_id, "")
        if checks:
            by_id[shot_id]["rule_checks"] = sorted(checks)
        projection = _cache_projection(by_id[shot_id])
        if cache_key and projection is not None:
            cache[cache_key] = projection

    def _apply_text(shot_id: str, items: list[dict[str, Any]],
                    payloads: list[dict[str, Any]], text: str) -> None:
        row = by_id[shot_id]
        if not payloads:
            # Low-resolution readings were the reason for this pass, so
            # they are dropped rather than kept as package evidence.
            row.update({"readable_claims": [], "pointed_claims": [], "text_agreed": [],
                        "pointed_agreed": [], "text_pass": "failed"})
            text_errors.append({"type": "VISION_TEXT_PASS_FAILED", "shot_id": shot_id,
                                "message": str(text)[-1000:]})
            return
        row["text_agreed"] = agreed_text(
            [_as_list(p.get("readable_claims")) for p in payloads])
        row["readable_claims"] = list(row["text_agreed"])
        row["text_reads"] = [_as_list(p.get("readable_claims")) for p in payloads]
        pointed = agreed_text([_as_list(p.get("pointed_text")) for p in payloads])
        row["pointed_agreed"] = pointed
        if pointed:
            row["pointing_to_text"] = True
        row["text_pass"] = "hires"
        row["text_frames"] = [{"path": item["path"], "timestamp": item["timestamp"]}
                              for item in items]
        _remember(shot_id)
        _save_vision_cache(cache_path, cache)

    def _attempts(phase: str, index: int, total: int, shot_ids: list[str]):
        started = time.time()

        def note(attempt: int, attempts: int, last_error: str) -> None:
            _progress(report_dir, phase=phase, index=index, total=total, shot_ids=shot_ids,
                      attempt=attempt, attempts=attempts, last_error=last_error or None,
                      request_started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      step_elapsed_s=round(time.time() - started, 1))
        return note

    def _checkpoint() -> None:
        # A partial file must not claim the previous attempt's batch count,
        # and must never claim ok.
        _set_cache_summary(
            _vision_summary(profile, batches, responses, vision_errors, partial=True),
            partial=True)
        analysis["ok"] = False
        _persist(report_dir, analysis)

    _progress(report_dir, phase="sheet_prepare", index=0, total=len(batches),
              prepare_done=0, prepare_total=len(batches),
              vision_left=len(batches), text_left=len(text_requests), started_at=
              time.strftime("%Y-%m-%dT%H:%M:%S%z"), finished_at=None)
    with private_temp_dir(prefix="jy_vision_", dir=report_dir) as temp:
        temp_dir = Path(temp)
        schema = _write_schema(temp_dir)
        def _call_batch(job: tuple[int, list[dict[str, Any]], Path]) -> tuple[int, tuple[bool, str]]:
            batch_index, batch, response_file = job
            result = _run_vision_with_retry(
                temp_dir / f"sheet_{batch_index:04d}.jpg",
                _vision_prompt(batch, batch_index, len(batches), analysis_rules),
                response_file,
                profile=profile,
                timeout=timeout,
                workdir=temp_dir,
                schema=schema,
                on_attempt=_attempts("vision", batch_index, len(batches),
                                     sorted({str(item["shot_id"]) for item in batch})),
            )
            return batch_index, result

        def _prepare_batch(job):
            batch_index, batch = job
            sheet = temp_dir / f"sheet_{batch_index:04d}.jpg"
            tiles = [{**item, "label": f"#{n} {item['timestamp']:.1f}s"}
                     for n, item in enumerate(batch, 1)]
            written = _write_sheet(tiles, sheet, ffmpeg=ffmpeg, fit=True, font_size=22,
                                   **_sheet_geometry(batch))
            return batch_index, (batch_index, batch,
                                  temp_dir / f"response_{batch_index:04d}.txt" if written else None)

        stage_started = time.monotonic()
        prepared = _prepare_indexed(list(enumerate(batches, 1)), _prepare_batch,
            on_done=lambda done, total: _progress(report_dir, prepare_done=done, prepare_total=total))
        stage_seconds["sheet_prepare"] = round(time.monotonic() - stage_started, 3)
        jobs = [prepared[index] for index in sorted(prepared) if prepared[index][2] is not None]
        for batch_index, _, response_file in prepared.values():
            if response_file is None:
                vision_errors.append({"type": "VISION_SHEET_FAILED", "batch": batch_index})
        workers = _batch_workers(len(jobs))
        _progress(report_dir, phase="vision", batch_workers=workers, transport=_vision_transport(),
                  requests_finished=0, vision_left=len(jobs))
        _checkpoint()

        stage_started = time.monotonic()
        for done, (batch_index, (ok, text)) in enumerate(_iter_indexed(jobs, _call_batch, workers), 1):
            _, batch, _ = prepared[batch_index]
            _progress(report_dir, requests_finished=done, vision_left=len(jobs) - done)
            if not ok:
                vision_errors.append({"type": "VISION_CALL_FAILED", "batch": batch_index,
                                      "message": text[-4000:]})
                _checkpoint()
                continue
            try:
                payload = _json_from_text(text)
                rows = payload if isinstance(payload, list) else payload.get("shots", [])
                if not isinstance(rows, list):
                    raise ValueError("response JSON is not a shot array")
            except (ValueError, TypeError, AttributeError) as exc:
                vision_errors.append({"type": "VISION_JSON_INVALID", "batch": batch_index,
                                      "message": str(exc), "response": text[-2000:]})
                _checkpoint()
                continue
            responses.append({"batch": batch_index, "shot_count": len(rows)})
            tile_map = {n: item for n, item in enumerate(batch, 1)}
            batch_shots = {str(item["shot_id"]) for item in batch}
            seen: set[str] = set()
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                shot_id = str(raw.get("shot_id") or raw.get("shot_label") or "").strip()
                if shot_id not in batch_shots or shot_id not in by_id or shot_id in seen:
                    continue
                seen.add(shot_id)
                row = by_id[shot_id]
                description = str(raw.get("description") or "").strip()
                visual_tags = _as_list(raw.get("visual_tags"))
                evidence_tags = _as_list(raw.get("evidence_tags"))
                action_complete = raw.get("action_complete") is True
                confidence = max(0.0, min(1.0, _number(raw.get("confidence"), 0.0)))
                if not description or not visual_tags or not evidence_tags or confidence <= 0:
                    row["status"] = "pending_analysis"
                    row["analysis_error"] = "vision response lacks auditable description/tags/confidence"
                    continue
                row.update({
                    "description": description,
                    "visual_description": description,
                    "visual_tags": visual_tags,
                    "evidence_tags": evidence_tags,
                    "action_complete": action_complete,
                    "head_waste": max(0.0, min(1.0, _number(raw.get("head_waste")))),
                    "tail_waste": max(0.0, min(1.0, _number(raw.get("tail_waste")))),
                    "analysis_confidence": confidence,
                    "analysis_source": _contact_sheet_source(),
                    "status": "ready_for_matching" if action_complete else "pending_analysis",
                    # DEMO_ACTIONS V0.2 素材侧字段。缺失时**不写键**（而不是写
                    # None）—— demo_actions.judge() 靠「键不存在」区分「旧分析结果」
                    # 与「模型明确说了没有动作/没有主体」，写 None 会把后者也当成
                    # 未覆盖而放行。
                    **_demo_action_fields(raw),
                })
                try:
                    from . import rulepack as _rp
                    _rp.annotate_shot_labels(row, _rp.for_manifest(manifest))
                except Exception:
                    pass
                frames = shot_frames.get(shot_id) or _shot_frames(row)
                row["vision_frames"] = []
                for f in frames:
                    entry = {"path": f["path"], "timestamp": f["timestamp"]}
                    if f.get("quality"):
                        entry["quality"] = f["quality"]
                    if f.get("replaced"):
                        entry["replaced"] = True
                    row["vision_frames"].append(entry)
                row["action_segments"] = _action_segments(raw.get("action_segments"), row)
                if isinstance(raw.get("evidence_time"), (int, float)):
                    frame = _nearest_frame(frames, float(raw["evidence_time"]))
                else:
                    picked = tile_map.get(_label_number(raw.get("evidence_frame")))
                    frame = picked if picked and picked["shot_id"] == shot_id else None
                    frame = frame or next(iter(frames), None)
                row["evidence_intervals"], from_model = _primary_interval(
                    raw, row, frames, frame, evidence=bool(evidence_tags))
                if (not action_complete and from_model and _certified_segment(row)
                        and row.get("has_subject") is not False
                        and row.get("evidence_strength") in ("A", "B")):
                    # 模型整条判未完成，但审核帧落在一段写明了结果的动作里；
                    # 切的就是这段。进行中、没有结果的段不能顶替完成。
                    action_complete = True
                    row.update({"action_complete": True, "status": "ready_for_matching",
                                "action_complete_source": "evidence_interval"})
                else:
                    row["action_complete_source"] = "model"
                if not action_complete:
                    # 视觉结果本身已通过校验（description/tags/confidence 齐全），
                    # 这里只是动作状态不达标。必须显式写明原因，否则会被下面的
                    # setdefault 盖上 "no validated vision result" —— 那行字会让人
                    # 误以为视觉分析失败，实际不是。
                    row["analysis_error"] = (
                        "action_complete=false：模型判定该镜头动作未完成或画面非稳定展示状态，未进入匹配池")
                else:
                    row.pop("analysis_error", None)
                wanted = [tile_map[n] for n in (_label_number(v) for v in _as_list(raw.get("text_tiles")))
                          if n in tile_map and tile_map[n]["shot_id"] == shot_id]
                if not wanted and (row.get("pointing_to_text") or row.get("readable_claims")
                                   or row.get("role") in ("product_display", "CTA")):
                    wanted = [frame] if frame else []
                if wanted and action_complete and _text_pass_enabled():
                    text_requests[shot_id] = wanted[:2]
                    _hold_text(row, wanted[:2])
                else:
                    _remember(shot_id)
            # Persist after every completed vision batch so a later timeout
            # does not discard the understanding work that already finished.
            _save_vision_cache(cache_path, cache)
            _checkpoint()
        stage_seconds["vision_api_and_checkpoint"] = round(time.monotonic() - stage_started, 3)
        if batches:
            _checkpoint()
        text_total = len(text_requests)
        _progress(report_dir, phase="text_prepare", vision_left=0, text_left=text_total,
                  prepare_done=0, prepare_total=text_total, batch_workers=0)

        def _prepare_text(job):
            text_index, (shot_id, items) = job
            sheet = temp_dir / f"text_{text_index:04d}.jpg"
            tiles = [{**item, "label": f"#{n} {item['timestamp']:.1f}s"}
                     for n, item in enumerate(items, 1)]
            written = _write_sheet(tiles, sheet, ffmpeg=ffmpeg, fit=True, font_size=26,
                                   **_sheet_geometry(items, text=True))
            return text_index, (text_index, shot_id, items, sheet if written else None)

        stage_started = time.monotonic()
        text_prepared = _prepare_indexed(list(enumerate(text_requests.items(), 1)), _prepare_text,
            on_done=lambda done, total: _progress(report_dir, prepare_done=done, prepare_total=total))
        stage_seconds["text_prepare"] = round(time.monotonic() - stage_started, 3)

        def _read_text(job):
            text_index, shot_id, items, sheet = job
            text = "VISION_TEXT_SHEET_FAILED"
            payloads: list[dict[str, Any]] = []
            if sheet is not None:
                for read in range(env_int("JY_VISION_TEXT_READS", 2, minimum=1)):
                    ok, text = _run_vision_with_retry(
                        sheet, _text_prompt(shot_id, items),
                        temp_dir / f"text_{text_index:04d}_{read + 1}.txt",
                        profile=profile, timeout=timeout, workdir=temp_dir,
                        on_attempt=_attempts(f"text_read_{read + 1}", text_index,
                                             text_total, [shot_id]))
                    payload: Any = None
                    if ok:
                        try:
                            payload = _json_from_text(text)
                        except (ValueError, TypeError):
                            payload = None
                        if isinstance(payload, list):
                            payload = next((p for p in payload if isinstance(p, dict)), None)
                    if not isinstance(payload, dict):
                        payloads = []
                        break
                    payloads.append(payload)
            return text_index, (shot_id, items, payloads, text)

        # 每个镜头的复核互不依赖，并发读；总并发仍受跨进程 vision 槽位约束。
        # 结果只在主线程落到行上、写缓存与断点，避免多线程同时改同一份分析。
        workers = min(env_int("JY_VISION_TEXT_WORKERS", _requested_batch_workers(), minimum=1),
                      _requested_batch_workers(), max(1, text_total))
        _progress(report_dir, phase="text", batch_workers=workers, requests_finished=0)
        text_jobs = [text_prepared[index] for index in sorted(text_prepared)]
        stage_started = time.monotonic()
        for done, (_, value) in enumerate(_iter_indexed(text_jobs, _read_text, workers), 1):
            _apply_text(*value)
            _progress(report_dir, text_left=text_total - done, requests_finished=done)
            _checkpoint()
        stage_seconds["text_api_and_checkpoint"] = round(time.monotonic() - stage_started, 3)
    _annotate_shot_labels(manifest, shots)
    apply_cross_shot_text(shots)
    analysis["text_pass"] = {"requested": len(text_requests),
                             "failed": len(text_errors), "errors": text_errors}
    # Do not silently retain the placeholder output produced by the old flow.
    for row in shots:
        if row.get("status") != "ready_for_matching":
            row["status"] = "pending_analysis"
            row.setdefault("analysis_error", "no validated vision result")
    analysis["shots"] = shots
    _set_cache_summary(_vision_summary(profile, batches, responses,
                                       vision_errors, partial=False), partial=False)
    analysis["pending_items"] = list(analysis.get("pending_items", [])) + vision_errors
    analysis["ok"] = bool(shots) and not analysis["pending_items"] and not any(
        needs_vision(row) for row in shots)
    _persist(report_dir, analysis)
    _progress(report_dir, phase="done", vision_left=0, text_left=0,
              finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    return analysis


def _segments_text(shot: dict[str, Any]) -> str:
    return "；".join(
        f"{_number(seg.get('start')):.1f}-{_number(seg.get('end')):.1f}s {seg.get('action')}"
        + (f"→{seg['result']}" if seg.get("result") else "")
        for seg in shot.get("action_segments") or [] if isinstance(seg, dict))


def match_with_codex(manifest: dict[str, Any], analysis: dict[str, Any], report_dir: Path,
                     *, profile: str | None = None, timeout: int | None = None) -> dict[str, Any]:
    """Use the same observed shot descriptions to rank each spoken sentence."""
    if timeout is None:
        timeout = env_int("JY_VISION_TIMEOUT", 600, minimum=60)
    profile = _clean_profile(
        profile if profile is not None else os.environ.get("JY_VISION_PROFILE", "volc"))
    from . import timing_contract
    sentence_rows = timing_contract.split_sentences(manifest)
    claims = [{"index": i, "text": timing_contract.sentence_text(row)}
              for i, row in enumerate(sentence_rows, 1)]
    shots = [s for s in analysis.get("shots", []) if s.get("status") == "ready_for_matching"]
    if not claims or not shots:
        analysis["semantic_matches"] = []
        return analysis
    catalog = "\n".join(
        f"{s['shot_id']} | {s.get('source_start', 0):.3f}-{s.get('source_end', 0):.3f}s | "
        f"描述:{s.get('description','')} | 画面标签:{'、'.join(_as_list(s.get('visual_tags')))} | "
        f"证据:{'、'.join(_as_list(s.get('evidence_tags')))} | action_complete={s.get('action_complete')} | "
        f"action_phase={s.get('action_phase') or ''} | "
        f"角色:{s.get('role') or ''} | 证据强度:{s.get('evidence_strength') or ''} | "
        f"观察置信度:{s.get('analysis_confidence', '')} | 可见结果:{s.get('result') or ''} | "
        f"动作分段:{_segments_text(s)} | 包装可读文字:{'、'.join(_as_list(s.get('readable_claims')))} | "
        f"头尾废料={_number(s.get('head_waste')):.2f}/{_number(s.get('tail_waste')):.2f}"
        for s in shots
    )
    from . import demo_actions, rulepack, shot_notes
    segments = sentence_rows
    category = str(manifest.get("category") or "")
    claim_lines = []
    for c in claims:
        seg = segments[c["index"] - 1] if 0 < c["index"] <= len(segments) else None
        note = shot_notes.note_for(manifest, c["index"], seg)
        claim_text = str((seg or {}).get("claim_text") or "")
        proves = "；".join(
            req.name + (f"——{req.object}，{req.result}" if req.result else "")
            for req in demo_actions.requirements_for(c["text"], claim_text, category)
            if req.evidence_required)
        contract = demo_actions.sentence_contract(c["text"], claim_text, category=category,
                                                   product=str(manifest.get("product") or ""))
        context = " / ".join(row["text"] for row in claims[max(0, c["index"] - 2):c["index"] + 1])
        claim_lines.append(f"{c['index']}. {c['text']}"
                           + (f"（卖点：{proves}）" if proves else "")
                           + (f"（画面要求：{note}）" if note else "")
                           + f"\n   句子合同：{contract['kind']}；上下文：{context}")
    claim_text = "\n".join(claim_lines)
    rules = rulepack.prompt_rules(manifest, "match")
    rules += [f"整条视频的画面要求：{g}" for g in manifest.get("shot_notes_general") or []]
    rule_text = ("\n剪辑规则（必须遵守）：\n" + "\n".join(f"- {r}" for r in rules) + "\n") if rules else ""
    prompt = f"""你是剪映脚本到镜头的匹配器。只使用下面已经由视觉模型观察到的镜头描述，不得编造描述或选择列表以外的 shot_id。
每句口播必须匹配真实画面：画面在展示什么，口播就应讲什么。对没有直接证据的句子返回空 top3，不要用产品无关空镜凑数。输出严格 JSON 数组：
[{{"index":1,"top3":[{{"shot_id":"...","score":0,"matched_claims":["..."],"reason":"..."}}]}}]
score 0-100；只有明确匹配并且证据足够才给 70 以上；`matched_claims` 必须写观察到的画面事实，不要把口播原文复制成证据。
先通读完整口播，再结合前后句给每句打分；不要只拿这句里的一个词检索整条素材。
90-100：画面直接展示该句要求的动作、部位或可读标称，过程与结果足够；70-89：同主题且满足这句的证据要求；不足70：缺少关键动作/结果、只有关联、观察不确定或无关。分数不能替代证据。
无功能卖点的开场、共鸣和衔接句可以正常配同商品的手持入画、拆包或本体展示；这类展示不证明相邻功能句。价格/囤货仍要求量感画面。
荧光、透气、吸收等实验可由相邻且语义兼容的多句共同承担，接同一素材的后续过程，不能每句从头重播；独立的侧漏、面层、品牌或价格句要换到对应画面。
逐句比较所有合格候选，再让重复/机位变化在同等证据且分数接近的候选中决定顺序。包装文字不能冒充功能实验；检测仪本身不证明已出现有效读数。
每个候选的 reason 写明可见事实、未满足内容及建议用哪段动作；若有反证请返回 contradictions 数组并给低于70分，不得为填满Top3虚构事实。
句子后面括号里的「卖点」是这句要用画面证明的事，按卖点选镜，不要按口播字面词去对画面。
句子后面括号里的「画面要求」是运营写的备注，候选镜头要尽量满足它，满足不了的在 reason 里写明。
{rule_text}
口播句：
{claim_text}

已分析镜头：
{catalog}
"""
    report_dir.mkdir(parents=True, exist_ok=True)
    attempts = env_int("JY_VISION_RETRIES", 4, minimum=1)
    failure = ""
    stderr_tail = ""
    used = 0
    for attempt in range(attempts):
        used = attempt + 1
        with private_temp_dir(prefix="jy_match_", dir=report_dir) as temp:
            temp_dir = Path(temp)
            response = temp_dir / "response.txt"
            prompt_path = temp_dir / "prompt.txt"
            prompt_path.write_text(prompt, encoding="utf-8")
            if _vision_transport() == "volc_glm":
                with _vision_slot():
                    ok, text = _run_volc_chat(None, prompt, response, timeout=timeout)
                stderr_tail = ""
                if ok:
                    raw_text, error, code = text, "", 0
                else:
                    raw_text, error, code = "", text, 1
            else:
                # Text-only call: no image attachment.  v1.3.20 routes it through the
                # same file-backed runner as the vision batches.  It previously kept
                # the old stdout=PIPE pattern, which is the same deadlock v1.3.17
                # fixed in _run_vision and left behind here.
                with _vision_slot():
                    ok, code, error = _run_codex_file(
                        _codex_command(None, response, profile, workdir=temp_dir),
                        prompt_path, temp_dir / "stdout.log", temp_dir / "stderr.log", timeout)
                stderr_tail = _tail(temp_dir / "stderr.log", 2000).strip()
                if response.is_file() and response.stat().st_size:
                    raw_text = response.read_text(encoding="utf-8", errors="replace")
                elif ok:
                    raw_text = _tail(temp_dir / "stdout.log")
                else:
                    raw_text = ""
            if error:
                failure = f"match call {error}"
            elif not raw_text.strip():
                failure = (f"match call codex exit {code}: {stderr_tail[-800:]}" if not ok
                           else f"match call {EMPTY_RESPONSE}")
            else:
                try:
                    payload = _json_from_text(raw_text)
                    matches = payload if isinstance(payload, list) else payload.get("matches", [])
                    failure = ""
                    # 偶发的「每句都给空候选」不是判断结果，按空响应重试。
                    if (len(claims) > 1 and isinstance(matches, list)
                            and not any(isinstance(m, dict) and m.get("top3") for m in matches)):
                        failure = f"match call {EMPTY_RESPONSE}: every sentence has no candidate"
                except (ValueError, TypeError, AttributeError) as exc:
                    failure = f"match response invalid: {exc}"
        if not failure:
            analysis["semantic_matches"] = matches
            analysis.pop("semantic_match_error", None)
            break
        if attempt >= attempts - 1 or not _is_transient(failure):
            break
        time.sleep(_backoff_seconds(attempt))
    if failure:
        analysis["semantic_match_error"] = failure
        analysis["semantic_matches"] = []
    (report_dir / "shot_match_ai.json").write_text(
        json.dumps({"claims": claims, "matches": analysis.get("semantic_matches", []),
                    "error": analysis.get("semantic_match_error"),
                    "attempts": used,
                    "stderr_tail": stderr_tail[-2000:] if failure else ""},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return analysis
