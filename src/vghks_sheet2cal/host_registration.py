"""Stable local-machine identity; copied state does not grant another host access."""
import hashlib
import os
from pathlib import Path


def current_host_id():
    try:
        machine = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ValueError("無法讀取本機識別，監控未啟動。") from exc
    if not machine:
        raise ValueError("本機識別為空，監控未啟動。")
    return hashlib.sha256(f"vghks-sheet2cal:{machine}:{os.getuid()}".encode()).hexdigest()
