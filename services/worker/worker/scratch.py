from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from mf_core.config import Settings
from mf_core.errors import ResourceError
from mf_core.telemetry import get_logger

log = get_logger("mf.scratch")


def job_dir(s: Settings, job_id: uuid.UUID | str) -> Path:
    p = Path(s.scratch) / "jobs" / str(job_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(str(path)).free


def ensure_space(s: Settings, path: Path, needed: int) -> None:
    reserve = s.scratch_min_free_gb * 1024**3
    have = free_bytes(path)
    if have - needed < reserve:
        raise ResourceError(
            f"мало места на scratch: свободно {have // 1024**2} MiB, нужно {needed // 1024**2} MiB",
            free=have, needed=needed,
        )


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def gc(s: Settings, keep: set[str], ttl_s: int = 86_400) -> int:
    root = Path(s.scratch) / "jobs"
    if not root.is_dir():
        return 0
    removed = 0
    now = time.time()
    for p in root.iterdir():
        if not p.is_dir() or p.name in keep:
            continue
        try:
            if now - p.stat().st_mtime > ttl_s:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("scratch_gc", removed=removed)
    return removed