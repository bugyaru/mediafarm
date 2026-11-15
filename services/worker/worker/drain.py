"""preStop-хук: kubectl exec python -c 'import worker.drain as d; d.mark_draining()'."""
from __future__ import annotations

from pathlib import Path

from mf_core.config import Settings, get_settings

SENTINEL = ".draining"


def _path(s: Settings) -> Path:
    return Path(s.scratch) / SENTINEL


def mark_draining() -> None:
    s = get_settings()
    p = _path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("draining", encoding="utf-8")
    print(f"marked draining: {p}")


def is_draining(s: Settings | None = None) -> bool:
    return _path(s or get_settings()).exists()


def clear() -> None:
    _path(get_settings()).unlink(missing_ok=True)