"""A reentrant thread lock plus an OS lock per spreadsheet on this host."""
import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .locking import lock_file

_locks = {}
_guard = threading.Lock()
_local = threading.local()


@contextmanager
def sheet_mutex(spreadsheet_id, timeout=30):
    key = hashlib.sha256(str(spreadsheet_id).encode()).hexdigest()
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=timeout):
        raise TimeoutError("另一個程序正在更新此刀表，稍後重試。")
    handle = None
    try:
        held = getattr(_local, "held", set())
        if key in held:
            yield
            return
        directory = Path(os.environ.get("VGHKS_SHEET_LOCK_DIR", Path.home() / ".cache/vghks-sheet-locks"))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(directory / (key + ".lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        handle = os.fdopen(fd, "a+b")
        deadline = time.monotonic() + timeout
        while True:
            try:
                lock_file(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("另一個程序正在更新此刀表，稍後重試。") from exc
                time.sleep(0.05)
        _local.held = held | {key}
        try:
            yield
        finally:
            _local.held = held
    finally:
        if handle:
            handle.close()
        lock.release()
