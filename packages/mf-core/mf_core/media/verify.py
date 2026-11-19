from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mf_core.config import Settings
from mf_core.errors import VerifyFailed
from mf_core.media.probe import MediaInfo, ffprobe

_CMP = re.compile(r"^\s*([a-z_]+)\s*(<=|>=|==|<|>)\s*([\d.]+)\s*$")
_FLAGS = {"has_video", "has_audio", "no_audio", "has_subtitles"}

def _metrics(info: MediaInfo, path: Path | None) -> dict[str, float]:
    v = info.video
    return {
        "duration": info.duration or (v.duration if v else 0) or 0,
        "size": float(info.size or (path.stat().st_size if path and path.exists() else 0)),
        "bitrate": float(info.bitrate or 0),
        "width": float(v.width if v else 0),
        "height": float(v.height if v else 0),
        "fps": float(v.fps if v and v.fps else 0),
        "audio_streams": float(len(info.audio)),
        "has_video": 1.0 if info.has_video else 0.0,
        "has_audio": 1.0 if info.has_audio else 0.0,
        "no_audio": 0.0 if info.has_audio else 1.0,
        "has_subtitles": 1.0 if info.subtitle_indexes else 0.0,
    }

async def verify_output(
        output: str, *, checks: list[str], src_info: MediaInfo | None = None,
        local_path: str | None = None, settings: Settings | None = None,
        on_log: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    info = await ffprobe(output, settings=settings, on_log=on_log)
    path = Path(local_path) if local_path else None
    m = _metrics(info, path)
    failures: list[str] = []

    for chk in checks or []:
        if chk in _FLAGS:
            if m[chk] < 1:
                failures.append(chk)
            continue
        if chk.startswith("duration_delta"):
            mm = _CMP.match(chk)
            if mm and src_info and src_info.duration:
                delta = abs(m["duration"] - src_info.duration)
                if not _cmp(delta, mm.group(2), float(mm.group(3))):
                    failures.append(f"{chk} (факт {delta:.3f})")
            continue
        mm = _CMP.match(chk)
        if not mm:
            failures.append(f"неизвестная проверка: {chk}")
            continue
        key, op, val = mm.group(1), mm.group(2), float(mm.group(3))
        if key not in m:
            failures.append(f"неизвестная метрика: {key}")
        elif not _cmp(m[key], op, val):
            failures.append(f"{chk} (факт {m[key]})")

    if failures:
        raise VerifyFailed("проверка результата не пройдена", failures=failures, metrics=m)
    return {"metrics": m, "probe": info.to_dict(), "checks": len(checks or [])}

def _cmp(a: float, op: str, b: float) -> bool:
    return {"<=": a <= b, ">=": a >= b, "<": a < b, ">": a > b, "==": abs(a - b) < 1e-6}[op]