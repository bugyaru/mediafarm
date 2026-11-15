from __future__ import annotations

import time
from fnmatch import fnmatch

DEFAULT_EXCLUDE = [
    "*.part", "*.part-*", "*.tmp", "*.temp", "*.crdownload", "*.!ut", "~*", ".*",
]


def match_globs(name: str, path: str, include: list[str], exclude: list[str]) -> bool:
    patterns = list(exclude or []) + DEFAULT_EXCLUDE
    if any(fnmatch(name, p) or fnmatch(path, p) for p in patterns):
        return False
    inc = include or ["*"]
    return any(fnmatch(name, p) or fnmatch(path, p) for p in inc)


class StabilityTracker:
    """Файл «дозрел», если size+mtime не менялись stability_s секунд."""

    def __init__(self, stability_s: int, ttl_s: int = 3600) -> None:
        self.stability_s = max(0, stability_s)
        self.ttl_s = ttl_s
        self._seen: dict[str, tuple[int, float, float]] = {}

    def is_stable(self, key: str, size: int, mtime: float, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        prev = self._seen.get(key)
        if prev is None or prev[0] != size or prev[1] != mtime:
            self._seen[key] = (size, mtime, now)
            return self.stability_s == 0
        return (now - prev[2]) >= self.stability_s

    def forget(self, key: str) -> None:
        self._seen.pop(key, None)

    def prune(self, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        stale = [k for k, v in self._seen.items() if now - v[2] > self.ttl_s]
        for k in stale:
            self._seen.pop(k, None)
        return len(stale)

    def pending(self) -> int:
        return len(self._seen)