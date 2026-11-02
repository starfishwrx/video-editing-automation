"""跨进程计数槽位：限制同时占用 GPU 会话、视觉模型等共享资源的调用数。

每个槽位是 ``<slot_dir>/<name>.<i>.lock`` 文件上的一把独占字节锁。锁随文件句柄
释放，进程崩溃时由操作系统自动回收，不会留下需要人工删除的"死锁文件"。
同一进程里的多个线程各自打开句柄，同样互斥。
"""
from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def default_slot_dir() -> Path:
    raw = os.environ.get("JY_SLOT_DIR", "").strip()
    root = Path(raw) if raw else Path(tempfile.gettempdir()) / "jy_slots"
    root.mkdir(parents=True, exist_ok=True)
    return root


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


def env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


class SlotHandle:
    """``acquired`` 为 False 表示排队超时，调用方应走降级路径。"""

    def __init__(self, name: str, index: Optional[int], waited: float):
        self.name = name
        self.index = index
        self.waited = waited

    @property
    def acquired(self) -> bool:
        return self.index is not None


@contextmanager
def slot(name: str, capacity: int, *, timeout: float = 120.0,
         poll: float = 0.2, slot_dir: Optional[Path] = None) -> Iterator[SlotHandle]:
    """占用 ``name`` 资源的一个槽位。``capacity<=0`` 表示不限流，直接放行。"""
    if capacity <= 0:
        yield SlotHandle(name, -1, 0.0)
        return
    root = Path(slot_dir) if slot_dir else default_slot_dir()
    root.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    deadline = start + max(0.0, timeout)
    fd: Optional[int] = None
    index: Optional[int] = None
    while True:
        for i in range(capacity):
            path = root / f"{name}.{i}.lock"
            try:
                candidate = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o666)
            except OSError:
                continue
            if _try_lock(candidate):
                fd, index = candidate, i
                break
            os.close(candidate)
        if fd is not None or time.monotonic() >= deadline:
            break
        time.sleep(poll)
    handle = SlotHandle(name, index, time.monotonic() - start)
    try:
        yield handle
    finally:
        if fd is not None:
            _unlock(fd)
            os.close(fd)
