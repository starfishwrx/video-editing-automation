"""Read process identity without sending signals, including on Windows."""
from __future__ import annotations

import psutil


def assert_process_alive(pid: int, created_at: float | None = None) -> None:
    """Raise only when absent/reused; access denied must never authorize cleanup."""
    pid = int(pid)
    if pid <= 0:
        raise ProcessLookupError(f"invalid process id: {pid}")
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            raise ProcessLookupError(f"process exited: {pid}")
        if created_at is not None and abs(process.create_time() - float(created_at)) > 0.01:
            raise ProcessLookupError(f"process id reused: {pid}")
    except (psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
        raise ProcessLookupError(f"process exited: {pid}") from exc
    except psutil.AccessDenied:
        # Treat unknown identity as occupied, never as permission to remove locks.
        return


def process_created_at(pid: int) -> float | None:
    try:
        return psutil.Process(int(pid)).create_time()
    except psutil.AccessDenied:
        return None
