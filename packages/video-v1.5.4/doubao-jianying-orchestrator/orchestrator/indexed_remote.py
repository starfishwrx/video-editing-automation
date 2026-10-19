"""Bounded remote JSON transport for indexed planning and material observation.

The shared ``vision`` slot is mandatory.  No failure of locking, transport or
JSON parsing may become an approval or trigger an unthrottled request.
MaterialStore leases belong to the caller, which rechecks facts under the lease.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from . import vision_analyzer as vision
from .json_extract import extract_json
from .proc_slots import env_float, env_int, slot


REMOTE_PROTOCOL = "indexed-remote-json-contact-sheet-v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _global_slot_dir() -> Path:
    explicit = os.environ.get("JY_SLOT_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    # A/B arms deliberately isolate TEMP; using tempfile here would silently
    # grant each arm two provider requests instead of sharing two globally.
    local = os.environ.get("LOCALAPPDATA", "").strip()
    root = Path(local).expanduser() if local else Path.home() / ("AppData/Local" if os.name == "nt" else ".cache")
    return (root / "doubao-jianying" / "indexed_remote_slots").resolve()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(_json(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _model_signature(profile: str, *, with_images: bool = True) -> dict[str, Any]:
    """Read only model routing fields; credentials never enter receipts."""
    signature = {"profile": profile,
                 "transport": os.environ.get("JY_VISION_TRANSPORT", "codex-file").strip() or "codex-file",
                 "model": os.environ.get("JY_VISION_MODEL", "").strip(),
                 "provider": os.environ.get("JY_VISION_PROVIDER", "").strip()}
    if not with_images:
        signature["transport"] = "codex-file"
    raw_home = os.environ.get("CODEX_HOME", "").strip()
    configuration = Path(raw_home).expanduser() / "config.toml" if raw_home else Path.home() / ".codex" / "config.toml"
    try:
        import tomllib
        data = tomllib.loads(configuration.read_text(encoding="utf-8-sig"))
        configured = (data.get("profiles") or {}).get(profile) or {}
        if not signature["model"]:
            signature["model"] = configured.get("model") or data.get("model") or ""
        if not signature["provider"]:
            signature["provider"] = configured.get("model_provider") or data.get("model_provider") or ""
        route = (data.get("model_providers") or {}).get(signature["provider"]) or {}
        # Hash the endpoint to identify routing changes without exposing it.
        signature["route_sha256"] = _digest({k: route.get(k) for k in ("base_url", "wire_api")})
        signature["reasoning_effort"] = configured.get("model_reasoning_effort") or data.get("model_reasoning_effort")
    except (OSError, ValueError, ImportError):
        signature["route_sha256"] = None
    if signature["transport"] in {"anthropic", "ccswitch", "ccswitch_anthropic"}:
        route = vision._ccswitch_env()
        signature["model"] = route.get("model") or signature["model"]
        signature["route_sha256"] = _digest(route.get("base_url") or "")
    return signature


def _images(request: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for index, raw in enumerate(request.get("images") or [], 1):
        row = dict(raw) if isinstance(raw, dict) else {"path": raw}
        path = Path(str(row.get("path") or row.get("frame_path") or "")).expanduser().resolve()
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"remote evidence image is missing: frame {index}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if row.get("sha256") and row["sha256"] != digest:
            raise ValueError(f"remote evidence image changed: frame {index}")
        result.append({**row, "path": str(path), "sha256": digest, "frame_number": index})
    if len(result) > 96:
        raise ValueError("contact sheet exceeds 96 frames; split the observation without dropping evidence")
    return result


def _image_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in ("sha256", "frame_number", "timestamp_us",
                                        "rendered_timestamp_us", "index", "material_id")}


def _contact_sheet(images: list[dict[str, Any]], output: Path) -> None:
    from PIL import Image, ImageDraw, ImageOps
    columns = min(6, len(images))
    tile_w, tile_h, label_h = 320, 384, 48
    rows = math.ceil(len(images) / columns)
    sheet = Image.new("RGB", (columns * tile_w, rows * (tile_h + label_h)), "#202020")
    draw = ImageDraw.Draw(sheet)
    for index, image in enumerate(images):
        left, top = (index % columns) * tile_w, (index // columns) * (tile_h + label_h)
        with Image.open(image["path"]) as original:
            frame = ImageOps.exif_transpose(original).convert("RGB")
            frame.thumbnail((tile_w - 8, tile_h - 8))
            sheet.paste(frame, (left + (tile_w-frame.width)//2, top + (tile_h-frame.height)//2))
            frame.close()
        label = (f"F{index+1:03d} source_us={image.get('timestamp_us', '?')} sentence={image.get('index', '?')}\n"
                 f"render_us={image.get('rendered_timestamp_us', '?')}")
        draw.multiline_text((left + 4, top + tile_h + 3), label, fill="white", spacing=3)
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=92, optimize=True)
    sheet.close()


class RemoteModel:
    def __init__(self, profile: str | None = None, report_dir: str | Path | None = None):
        self.profile = vision._clean_profile(profile if profile is not None else os.environ.get("JY_VISION_PROFILE", "volc"))
        self.report_dir = Path(report_dir).expanduser().resolve() if report_dir else None
        self.timeout_s = 120
        self.max_attempts = 2
        self.stats: dict[str, Any] = {"remote_requests": 0, "cache_hits": 0,
                                      "attempts": 0, "slot_wait_s": 0.0, "requests_by_stage": {}}
        self._stats_lock = threading.Lock()

    def _count(self, key: str, value: int | float = 1):
        with self._stats_lock:
            self.stats[key] += value

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict) or not str(request.get("prompt") or "").strip():
            raise ValueError("remote request requires a prompt")
        images = _images(request)
        signature = _model_signature(self.profile, with_images=bool(images))
        identity = {"protocol": REMOTE_PROTOCOL, "stage": str(request.get("stage") or ""),
                    "prompt": request["prompt"], "payload": request.get("payload") or {},
                    "images": [_image_identity(row) for row in images], "model": signature}
        key = _digest(identity)
        raw_output = request.get("output")
        if raw_output:
            output = Path(raw_output).expanduser().resolve()
        elif self.report_dir:
            output = self.report_dir / "model" / f"{key}.raw.json"
        else:
            raise ValueError("remote request requires output or report_dir")
        cache = output.parent / "remote_cache" / f"{key}.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        # Per-request lock avoids duplicate remote work across processes.  It is
        # separate from the mandatory global provider concurrency limit.
        wait = min(120.0, env_float("JY_INDEXED_REMOTE_SLOT_TIMEOUT", 120.0))
        with slot(f"indexed-request-{key}", 1, timeout=wait,
                  slot_dir=cache.parent / ".locks") as request_slot:
            self._count("slot_wait_s", request_slot.waited)
            if not request_slot.acquired:
                raise TimeoutError("indexed remote request lock timed out; remote call was not started")
            if cache.is_file():
                try:
                    record = json.loads(cache.read_text(encoding="utf-8-sig"))
                    response = record.get("response")
                    if (record.get("input_sha256") == key and isinstance(response, dict)
                            and record.get("response_sha256") == _digest(response)):
                        self._count("cache_hits")
                        _write(output, response)
                        return response
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            mapping = [_image_identity(row) for row in images]
            prompt = (str(request["prompt"]) + "\n\n只返回 JSON 对象，不调用其他工具。\n输入：\n" +
                      _json(request.get("payload") or {}) +
                      ("\n\n联系表按从左到右、从上到下排列。每格 F 编号与时间映射：\n" + _json(mapping) if images else ""))
            sheet = output.parent / f"{key}.contact.jpg"
            if images:
                _contact_sheet(images, sheet)
            _write(output.with_suffix(".transport.request.json"), {"input_sha256": key, **identity})
            last_error = "remote model failed"
            for attempt in range(1, self.max_attempts + 1):
                attempt_dir = output.parent / "remote_attempts" / key / f"{attempt:02d}_{uuid.uuid4().hex}"
                attempt_dir.mkdir(parents=True, exist_ok=True)
                response_file = attempt_dir / "response.json"
                with slot("vision", min(2, env_int("JY_VISION_SLOTS", 2, minimum=1)),
                          timeout=wait, slot_dir=_global_slot_dir()) as handle:
                    self._count("slot_wait_s", handle.waited)
                    if not handle.acquired:
                        raise TimeoutError("global vision slot timed out; remote call was not started")
                    self._count("attempts")
                    self._count("remote_requests")
                    with self._stats_lock:
                        stage = str(request.get("stage") or "unknown")
                        by_stage = self.stats["requests_by_stage"]
                        by_stage[stage] = by_stage.get(stage, 0) + 1
                    try:
                        if images:
                            ok, text = vision._run_vision(sheet, prompt, response_file, profile=self.profile,
                                                          timeout=self.timeout_s, workdir=attempt_dir)
                        else:
                            prompt_file = attempt_dir / "prompt.txt"
                            prompt_file.write_text(prompt, encoding="utf-8")
                            ok, code, error = vision._run_codex_file(
                                vision._codex_command(None, response_file, self.profile, workdir=attempt_dir),
                                prompt_file, attempt_dir / "stdout.log", attempt_dir / "stderr.log", self.timeout_s)
                            text = response_file.read_text(encoding="utf-8-sig") if ok and response_file.is_file() else ""
                            if not ok:
                                text = error or f"codex exit {code}"
                        if not ok:
                            raise RuntimeError(text or "remote model failed without a response")
                        response = extract_json(text, accept=lambda value: isinstance(value, dict))
                    except (OSError, ValueError, RuntimeError) as exc:
                        last_error = str(exc)
                        _write(attempt_dir / "failure.json", {"attempt": attempt, "error": last_error,
                                                               "formal_delivery_allowed": False})
                        continue
                _write(cache, {"input_sha256": key, "protocol": REMOTE_PROTOCOL,
                               "model": signature, "response": response,
                               "response_sha256": _digest(response)})
                _write(output, response)
                return response
            raise RuntimeError(f"indexed remote model failed after {self.max_attempts} attempts: {last_error}")

    def observe_material(self, request: dict[str, Any]) -> dict[str, Any]:
        """Same JSON contract, with caller-owned material lease and provenance."""
        return self({"stage": "material_observe", **request})
