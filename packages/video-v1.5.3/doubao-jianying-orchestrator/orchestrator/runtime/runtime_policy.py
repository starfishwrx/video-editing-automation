"""One host/account resource budget shared by every production entry point."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterator

from .proc_slots import SlotHandle, slot, _try_lock, _unlock


@dataclass(frozen=True)
class RuntimePolicy:
    task_workers: int = 4
    prep_workers: int = 2
    prep_slots: int = 2
    api_workers: int = 20
    vision_slots: int = 20
    request_timeout: float = 360
    qc_stage_timeout: float = 900
    task_timeout: float = 14400
    slot_dir: str = field(default_factory=lambda: str(Path(tempfile.gettempdir()) / "jy_slots"))

    def to_dict(self) -> dict:
        return asdict(self)


_LEGACY = {
    "task_workers": "JY_TASK_WORKERS",
    "prep_workers": "JY_VISION_PREP_WORKERS",
    "prep_slots": "JY_VISION_PREP_SLOTS",
    "api_workers": "JY_VISION_BATCH_WORKERS",
    "vision_slots": "JY_VISION_SLOTS",
    "request_timeout": "JY_VISION_TIMEOUT",
    "qc_stage_timeout": "JY_POST_QC_STAGE_TIMEOUT_S",
    "task_timeout": "JY_TASK_BUDGET_S",
}


def load_policy() -> RuntimePolicy:
    """Explicit policy is authoritative; legacy env can only reduce defaults.

    Missing/malformed explicit files fail closed instead of silently restoring
    an independently chosen account budget in a child process.
    """
    defaults = RuntimePolicy()
    path = os.environ.get("JY_CONCURRENCY_POLICY_FILE", "").strip()
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("concurrency policy must be a JSON object")
        unknown = set(data) - {field.name for field in fields(RuntimePolicy)}
        if unknown:
            raise ValueError("unknown concurrency policy fields: " + ",".join(sorted(unknown)))
    else:
        data = {}
        for key, name in _LEGACY.items():
            raw = os.environ.get(name, "").strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            # Zero meant unlimited in older callers. It must not disable limits.
            if value > 0:
                data[key] = min(value, getattr(defaults, key))
        if os.environ.get("JY_SLOT_DIR", "").strip():
            data["slot_dir"] = os.environ["JY_SLOT_DIR"].strip()
    values = defaults.to_dict()
    values.update(data)
    for key in _LEGACY:
        try:
            value = float(values[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid concurrency policy {key}") from exc
        if not 0 < value < float("inf"):
            raise ValueError(f"concurrency policy {key} must be finite and positive")
        values[key] = int(value) if key.endswith(("workers", "slots")) else value
        if values[key] <= 0:
            raise ValueError(f"concurrency policy {key} must be at least one")
    values["slot_dir"] = str(Path(values["slot_dir"]).expanduser().resolve())
    return RuntimePolicy(**values)


@contextmanager
def vision_slot(timeout: float | None = None) -> Iterator[SlotHandle]:
    policy = load_policy()
    with slot("vision", policy.vision_slots,
              timeout=_remaining_timeout(policy.request_timeout if timeout is None else timeout),
              slot_dir=Path(policy.slot_dir)) as handle:
        yield handle


@contextmanager
def media_slot(timeout: float | None = None) -> Iterator[SlotHandle]:
    policy = load_policy()
    with slot("vision_prepare", policy.prep_slots,
              timeout=_remaining_timeout(policy.task_timeout if timeout is None else timeout),
              slot_dir=Path(policy.slot_dir)) as handle:
        yield handle


def _remaining_timeout(timeout: float) -> float:
    for stage in ("STAGE", "TASK"):
        deadline = os.environ.get(f"JY_{stage}_DEADLINE_TS") or os.environ.get(f"JY_{stage}_DEADLINE_EPOCH")
        if deadline:
            timeout = min(timeout, float(deadline) - time.time())
    return max(0, timeout)


def active_vision_slots() -> int:
    """Sample native locks; abandoned lock files do not count as in flight."""
    policy = load_policy()
    busy = 0
    for index in range(policy.vision_slots):
        path = Path(policy.slot_dir) / f"vision.{index}.lock"
        try:
            fd = os.open(str(path), os.O_RDWR)
        except FileNotFoundError:
            continue
        except OSError:
            busy += 1
            continue
        try:
            if _try_lock(fd):
                _unlock(fd)
            else:
                busy += 1
        finally:
            os.close(fd)
    return busy
