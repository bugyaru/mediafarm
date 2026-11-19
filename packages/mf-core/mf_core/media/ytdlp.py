"""yt-dlp: скачивание web-источников с resume, прогрессом и разбором ошибок.

Полный лог (stdout + stderr, включая [debug] при MF_YTDLP_VERBOSE=true) уходит
в on_log → job_log → детали задачи в UI в реальном времени.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mf_core.config import Settings, get_settings
from mf_core.errors import (
    ConfigInvalidError, MFError, SourceInvalidError, TransientNetworkError, classify,
)
from mf_core.media.proc import LogCB, run_streaming

PROGRESS_PREFIX = "MF|"
_MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts", ".mp3", ".m4a", ".flv", ".avi"}

@dataclass(slots=True)
class YtDlpResult:
    path: str
    info: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    stderr_tail: list[str] = field(default_factory=list)

def build_cmd(
        url: str, *, outdir: str | Path, resume: bool = True, live: bool = False,
        live_from_start: bool = False, max_duration_s: int | None = None,
        concurrent_fragments: int = 4, cookies_file: str | None = None,
        archive_file: str | None = None, format_selector: str | None = None,
        playlist: bool = False, extra: list[str] | None = None,
        verbose: bool | None = None, settings: Settings | None = None,
) -> list[str]:
    s = settings or get_settings()
    cmd = [
        s.ytdlp_path, "--no-colors", "--newline", "--no-simulate",
        "--print", "after_move:filepath",
        "--progress-template",
        "download:MF|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
        "%(progress.speed)s|%(progress.eta)s|%(info.id)s",
        "--retries", "10", "--fragment-retries", "20", "--retry-sleep", "exp=1:60",
        "--socket-timeout", "20", "--concurrent-fragments", str(concurrent_fragments),
        "--paths", f"home:{outdir}", "-o", "%(id)s.%(ext)s",
        "--write-info-json", "--ffmpeg-location", s.ffmpeg_path,
        "--merge-output-format", "mkv",
    ]
    if s.ytdlp_verbose if verbose is None else verbose:
        cmd += ["--verbose"]
    cmd += ["--continue"] if resume else ["--no-continue"]
    cmd += ["--yes-playlist"] if playlist else ["--no-playlist"]
    if format_selector:
        cmd += ["-f", format_selector]
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    if archive_file:
        cmd += ["--download-archive", archive_file]
    if live and live_from_start:
        cmd += ["--live-from-start"]
    if max_duration_s:
        cmd += ["--download-sections", f"*0-{max_duration_s}"]
    cmd += list(extra or []) + [url]
    return cmd

def parse_progress(line: str) -> dict[str, Any] | None:
    if not line.startswith(PROGRESS_PREFIX):
        return None
    parts = line[len(PROGRESS_PREFIX):].split("|")
    if len(parts) < 5:
        return None

    def num(v: str) -> float | None:
        try:
            f = float(v)
            return None if f != f else f
        except (TypeError, ValueError):
            return None

    done, total, speed, eta = num(parts[0]), num(parts[1]), num(parts[2]), num(parts[3])
    pct = round(done / total * 100, 2) if done and total else None
    return {"bytes": done, "total": total, "pct": pct, "speed": speed,
            "eta_s": eta, "id": parts[4] or None}

_NET = ("HTTP Error 5", "HTTP Error 429", "Connection reset", "timed out", "Temporary failure",
        "Network is unreachable", "IncompleteRead", "fragment")
_SRC = ("HTTP Error 404", "HTTP Error 410", "Video unavailable", "Private video",
        "This video is not available", "removed by the uploader", "DRM")
_CFG = ("Unsupported URL", "Sign in to confirm", "cookies", "HTTP Error 403",
        "age-restricted", "Requested format is not available")

def classify_ytdlp(rc: int, tail: list[str]) -> MFError:
    text = "\n".join(tail[-60:])
    if any(p.lower() in text.lower() for p in _CFG):
        return ConfigInvalidError("yt-dlp: нужен доступ/cookies/другой формат", rc=rc, tail=tail[-30:])
    if any(p.lower() in text.lower() for p in _SRC):
        return SourceInvalidError("yt-dlp: источник недоступен", rc=rc, tail=tail[-30:])
    if any(p.lower() in text.lower() for p in _NET):
        return TransientNetworkError("yt-dlp: сетевая ошибка", rc=rc, tail=tail[-30:])
    return MFError(f"yt-dlp завершился с кодом {rc}", rc=rc, tail=tail[-30:])

def _find_output(outdir: Path, printed: list[str]) -> Path:
    for p in reversed(printed):
        cand = Path(p)
        if cand.is_file():
            return cand
    files = [p for p in outdir.rglob("*")
             if p.is_file() and p.suffix.lower() in _MEDIA_EXT and not p.name.endswith(".part")]
    if not files:
        raise SourceInvalidError("yt-dlp не создал медиафайл", outdir=str(outdir))
    return max(files, key=lambda p: p.stat().st_mtime)

async def download(
        url: str, *, outdir: str | Path, on_progress=None, on_log: LogCB | None = None,
        on_cmd: Callable[[list[str]], Any] | None = None,
        cancel: asyncio.Event | None = None, settings: Settings | None = None, **kw: Any,
) -> YtDlpResult:
    s = settings or get_settings()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(url, outdir=outdir, settings=s, **kw)
    printed: list[str] = []

    if on_cmd is not None:
        res = on_cmd(cmd)
        if asyncio.iscoroutine(res):
            await res

    def on_line(line: str) -> None:
        snap = parse_progress(line)
        if snap is not None:
            if on_progress is not None:
                res = on_progress(snap)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)  # noqa: RUF006
            return
        if line.startswith("/") and not line.startswith(PROGRESS_PREFIX):
            printed.append(line)

    log_file = str(outdir / "ytdlp.log")
    try:
        res = await run_streaming(cmd, on_line=on_line, on_log=on_log, cancel=cancel, settings=s,
                                  log_file=log_file, stall_timeout_s=max(s.stall_timeout_s, 180))
    except OSError as e:
        raise classify(e) from e
    if res.returncode != 0:
        raise classify_ytdlp(res.returncode, res.stderr_tail)

    path = _find_output(outdir, printed)
    info: dict[str, Any] = {}
    candidates = sorted(outdir.rglob("*.info.json"), key=lambda p: p.stat().st_mtime)
    if candidates:
        try:
            info = json.loads(candidates[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
    return YtDlpResult(str(path), info, res.elapsed_s, res.stderr_tail)