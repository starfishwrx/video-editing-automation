"""Opt-in Antigravity CLI image transport; never a fallback for another model.

Each invocation has a private minimal workspace and a new conversation. AGY
uses an independent machine-wide provider limit, explicitly separate from the
Codex/Anthropic two-slot limit. No login, location or proxy settings are changed.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
import uuid

from .proc_slots import env_float, env_int, slot

PROTOCOL = "agy-cli-image-file-json-v1"
DEFAULT_MODEL = "gemini-3.8-flash-high"
ALIASES = {"agy", "agy-cli", "antigravity"}
_held = threading.local()


def selected() -> bool:
    return os.environ.get("JY_VISION_TRANSPORT", "").strip().lower() in ALIASES


def _user_root() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local).expanduser() if local else Path.home() / ("AppData/Local" if os.name == "nt" else ".cache")


def slot_dir() -> Path:
    explicit = os.environ.get("JY_AGY_SLOT_DIR", "").strip()
    return (Path(explicit).expanduser() if explicit else _user_root() / "doubao-jianying" / "agy_cli_slots").resolve()


def concurrency() -> int:
    return min(20, env_int("JY_AGY_CONCURRENCY", 20, minimum=1))


@contextmanager
def provider_slot(*, timeout: float | None = None):
    """A nested call in the same worker reuses its existing provider lease."""
    current = getattr(_held, "handle", None)
    if current is not None:
        yield current
        return
    wait = min(120.0, env_float("JY_AGY_SLOT_TIMEOUT", 120.0)) if timeout is None else min(120.0, timeout)
    with slot("agy-cli", concurrency(), timeout=wait, slot_dir=slot_dir()) as handle:
        if handle.acquired:
            _held.handle = handle
        try:
            yield handle
        finally:
            if handle.acquired:
                _held.handle = None


def _binary() -> Path:
    raw = os.environ.get("JY_AGY_BIN", "").strip()
    if not raw:
        raise ValueError("AGY_NON_RETRYABLE[CONFIG]: JY_AGY_BIN must explicitly identify the native CLI")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise ValueError("AGY_NON_RETRYABLE[CONFIG]: configured AGY binary does not exist")
    return path


@lru_cache(maxsize=8)
def _binary_hash(path: str, size: int, modified: int, created: int) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def signature() -> dict:
    """Pure local route identity; never reads auth or calls the CLI/provider."""
    binary = _binary()
    stat = binary.stat()
    return {"transport": "agy-cli", "provider": "antigravity",
            "model": os.environ.get("JY_AGY_MODEL", "").strip() or DEFAULT_MODEL,
            "cli_sha256": _binary_hash(str(binary), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns),
            "image_protocol": PROTOCOL}


def is_non_retryable(message: str) -> bool:
    return str(message).startswith("AGY_NON_RETRYABLE[")


def _failure(logs: str, code: int | None) -> str:
    text = logs.casefold()
    if "user location is not supported" in text or "unsupported user location" in text:
        return "AGY_NON_RETRYABLE[LOCATION_NOT_SUPPORTED]: native CLI rejected the user location; see private logs"
    if any(marker in text for marker in ("http 400", "http400", "status code 400", "status_code: 400")):
        return "AGY_NON_RETRYABLE[INVALID_REQUEST]: native CLI returned HTTP 400; see private logs"
    if any(marker in text for marker in ("http 401", "http 403", "invalid model", "unknown model", "model not found")):
        return "AGY_NON_RETRYABLE[AUTH_OR_MODEL]: native CLI rejected authentication or model; see private logs"
    return f"AGY_CLI_FAILED[{code}]: see private stdout, stderr and events logs"


def _command(binary: Path, work: Path, model: str) -> list[str]:
    return [str(binary), "--print",
            "Read task.md completely. Inspect input.jpg with the native image tool and write only result.json.",
            "--model", model, "--mode", "accept-edits", "--sandbox", "--dangerously-skip-permissions",
            "--disable-slash-commands", "--new-project", "--json-schema", "schema.json",
            "--output-format", "json", "--print-timeout", "110s", "--log-file", str(work / "events.log")]


def _kill_own_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            # Popen starts a new session below, so this process group contains
            # only this invocation and its descendants.
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def run(sheet: Path, prompt: str, output: Path, *, timeout: float = 120) -> tuple[bool, str]:
    """One native CLI attempt; its caller owns the bounded retry policy."""
    try:
        identity = signature()
        binary = _binary()
    except (OSError, ValueError) as exc:
        return False, str(exc)
    if not sheet.is_file() or not sheet.stat().st_size:
        return False, "AGY_NON_RETRYABLE[IMAGE_MISSING]: evidence contact sheet is unavailable"
    with provider_slot() as handle:
        if not handle.acquired:
            return False, "AGY_NON_RETRYABLE[SLOT_TIMEOUT]: AGY request was not started"
        explicit = os.environ.get("JY_AGY_SCRATCH_DIR", "").strip()
        root = (Path(explicit).expanduser() if explicit else _user_root() / "doubao-jianying" / "agy_cli_scratch").resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / ".gitignore").write_text("*\n", encoding="utf-8")
        work = root / uuid.uuid4().hex
        work.mkdir(mode=0o700)
        (work / ".gitignore").write_text("*\n", encoding="utf-8")
        shutil.copyfile(sheet, work / "input.jpg")
        _write(work / "schema.json", {"type": "object"})
        (work / "task.md").write_text(
            "# Business task\n" + prompt + "\n\n# File execution contract\n"
            "Inspect input.jpg using the native image-reading tool. Only read task.md, input.jpg and schema.json "
            "in this scratch workspace. Business instructions forbidding other tools do not forbid these necessary "
            "image reads and the one result-file write. Do not browse, run terminal commands, inspect other directories, "
            "delegate or use previous conversations. Write exactly one JSON object to result.json conforming to schema.json. "
            "Modify no other file. If the image cannot be inspected, report uncertainty, never invent an observation. "
            "A completion summary or stdout alone does not fulfill this task.\n", encoding="utf-8")
        command = _command(binary, work, identity["model"])
        receipt = {"protocol": PROTOCOL, "model": identity, "scratch_dir": str(work),
                   "input_sha256": hashlib.sha256((work / "input.jpg").read_bytes()).hexdigest(),
                   "concurrency": concurrency(), "slot_dir": str(slot_dir()), "slot_wait_s": handle.waited,
                   "internal_timeout_s": 110, "outer_timeout_s": min(120.0, float(timeout)),
                   "formal_delivery_allowed": False}
        started, timed_out, code = time.monotonic(), False, None
        try:
            with (work / "stdout.txt").open("w", encoding="utf-8") as stdout, \
                    (work / "stderr.txt").open("w", encoding="utf-8") as stderr:
                kwargs = {"cwd": str(work), "stdin": subprocess.DEVNULL, "stdout": stdout, "stderr": stderr}
                if os.name == "nt":
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                else:
                    kwargs["start_new_session"] = True
                proc = subprocess.Popen(command, **kwargs)
                try:
                    code = proc.wait(timeout=min(120.0, float(timeout)))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_own_tree(proc)
                    code = proc.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            receipt["failure"] = f"AGY_LAUNCH_FAILED:{type(exc).__name__}"
        receipt.update(exit_code=code, timed_out=timed_out, elapsed_s=time.monotonic() - started)
        _write(work / "invocation.json", receipt)
        output.parent.mkdir(parents=True, exist_ok=True)
        _write(output.with_suffix(".agy.json"), receipt)
        if timed_out:
            return False, "AGY_TIMEOUT: own process tree terminated; no result accepted"
        logs = "\n".join((work / name).read_text(encoding="utf-8", errors="replace")[-24000:]
                         for name in ("stdout.txt", "stderr.txt", "events.log") if (work / name).is_file())
        error = _failure(logs, code)
        if code != 0 or is_non_retryable(error):
            return False, error
        result = work / "result.json"
        if not result.is_file() or not result.stat().st_size:
            return False, "AGY_RESULT_MISSING: native CLI did not create its required JSON file"
        try:
            response = json.loads(result.read_text(encoding="utf-8-sig"))
            if not isinstance(response, dict):
                raise ValueError("expected object")
        except (OSError, ValueError):
            return False, "AGY_RESULT_INVALID: result.json must contain one JSON object"
        _write(output, response)
        return True, output.read_text(encoding="utf-8")
