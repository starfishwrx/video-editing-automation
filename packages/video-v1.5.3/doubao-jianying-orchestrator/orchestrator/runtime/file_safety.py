"""多进程/多线程并发下的文件写入安全工具。

- ``file_lock``：``<path>.lock`` 旁路文件上的独占字节锁，进程退出时由系统回收；
- ``atomic_write_text`` / ``locked_json_update``：同目录临时文件 + ``os.replace``，
  剪映短暂占用文件（PermissionError）时退避重试；
- ``reserve_unique_dir`` / ``reserve_exact_dir``：用 ``os.mkdir`` 原子占位草稿目录，
  两个并发任务不会拿到同一个名字；
- ``private_temp_dir``：每次调用独立的临时目录，清理失败（子进程还占着文件）不抛异常。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

from .proc_slots import _try_lock, _unlock

PathLike = Union[str, "os.PathLike[str]"]


class SkipUpdate(Exception):
    """``locked_json_update`` 的 mutate 抛出它表示放弃写入，原文件不动。"""


def _lock_path(path: PathLike) -> Path:
    return Path(str(os.fspath(path)) + ".lock")


@contextmanager
def file_lock(path: PathLike, timeout: float = 30, *, poll: float = 0.05) -> Iterator[Path]:
    """对 ``<path>.lock`` 加跨进程独占锁；超时抛 ``TimeoutError``。锁文件本身保留不删。"""
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(timeout))
    fd: Optional[int] = None
    while True:
        candidate = None
        try:
            candidate = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o666)
        except OSError:
            candidate = None
        if candidate is not None:
            if _try_lock(candidate):
                fd = candidate
                break
            os.close(candidate)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"file lock busy: {lock}")
        time.sleep(poll)
    try:
        yield lock
    finally:
        _unlock(fd)
        os.close(fd)


def _replace_with_retry(src: str, dst: Path, retries: int) -> None:
    for attempt in range(max(1, retries)):
        try:
            os.replace(src, str(dst))
            return
        except PermissionError:
            if attempt >= retries - 1:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.5))


def atomic_write_text(path: PathLike, text: str, encoding: str = "utf-8", *,
                      retries: int = 10, inplace_fallback: bool = False) -> Path:
    """原子写文本。``inplace_fallback`` 为真时，替换一直被占用就退回直接覆盖写。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            _replace_with_retry(tmp, target, retries)
        except PermissionError:
            if not inplace_fallback:
                raise
            with open(target, "w", encoding=encoding) as handle:
                handle.write(text)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return target


def _read_json(path: Path, default: Any, attempts: int = 5) -> Any:
    for attempt in range(attempts):
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return json.loads(json.dumps(default if default is not None else {}))
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
            continue
        try:
            return json.loads(raw)
        except ValueError:
            if attempt >= attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
    return json.loads(json.dumps(default if default is not None else {}))


def locked_json_update(path: PathLike, mutate: Callable[[Any], Any], default: Any = None, *,
                       indent: Optional[int] = 2, ensure_ascii: bool = False,
                       timeout: float = 30, inplace_fallback: bool = True) -> Any:
    """加锁读-改-写 JSON。mutate 返回新数据，返回 None 表示就地修改；抛 ``SkipUpdate`` 则不写。

    读不到合法 JSON 时会短暂重试（对方可能正在写），仍失败则抛 ``ValueError``。
    """
    target = Path(path)
    with file_lock(target, timeout=timeout):
        data = _read_json(target, default)
        try:
            result = mutate(data)
        except SkipUpdate:
            return data
        if result is not None:
            data = result
        atomic_write_text(target, json.dumps(data, ensure_ascii=ensure_ascii, indent=indent),
                          inplace_fallback=inplace_fallback)
        return data


def _write_marker(directory: Path, marker: str, token: Optional[str]) -> dict:
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "token": token or uuid.uuid4().hex,
    }
    try:
        (directory / marker).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return payload


def reserve_exact_dir(root: PathLike, name: str, *, reserved_marker: str = ".reserved",
                      token: Optional[str] = None) -> Path:
    """原子占用 ``root/name``；已存在抛 ``FileExistsError``。"""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    target = base / name
    os.mkdir(target)
    _write_marker(target, reserved_marker, token)
    return target


def reserve_unique_dir(root: PathLike, base_name: str, *, start: int = 1,
                       pattern: str = "{base}-rev-{n}", reserved_marker: str = ".reserved",
                       token: Optional[str] = None, max_tries: int = 1000) -> Path:
    """从 ``start`` 起占用第一个不存在的 ``pattern`` 目录并返回。"""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    for n in range(int(start), int(start) + max_tries):
        target = base / pattern.format(base=base_name, n=n)
        try:
            os.mkdir(target)
        except FileExistsError:
            continue
        _write_marker(target, reserved_marker, token)
        return target
    raise FileExistsError(f"no free directory for {base_name} under {base}")


def read_reservation(directory: PathLike, reserved_marker: str = ".reserved") -> Optional[dict]:
    try:
        data = json.loads((Path(directory) / reserved_marker).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def release_reserved_dir(directory: PathLike, reserved_marker: str = ".reserved") -> bool:
    """只有目录里除占位标记外什么都没有时才删除，返回是否删除。"""
    target = Path(directory)
    try:
        entries = [p.name for p in target.iterdir()]
    except OSError:
        return False
    if any(name != reserved_marker for name in entries):
        return False
    try:
        if reserved_marker in entries:
            (target / reserved_marker).unlink()
        target.rmdir()
        return True
    except OSError:
        return False


def _remove_tree(path: Path, budget: float) -> bool:
    deadline = time.monotonic() + max(0.0, budget)
    delay = 0.1
    while True:
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() >= deadline:
                break
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 1.0)
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


@contextmanager
def private_temp_dir(prefix: str = "jy_", dir: Optional[PathLike] = None, *,
                     cleanup_timeout: float = 5.0) -> Iterator[Path]:
    """独立临时目录；退出时带退避删除，仍被占用就留在原地，绝不因清理抛异常。"""
    if dir is not None:
        Path(dir).mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(dir) if dir is not None else None))
    try:
        yield path
    finally:
        try:
            _remove_tree(path, cleanup_timeout)
        except Exception:
            pass
