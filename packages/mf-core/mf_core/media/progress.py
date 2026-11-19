"""Запуск ffmpeg/ffmbc: -progress pipe:1, watchdog, мягкая отмена ('q' → SIGTERM → SIGKILL).

on_progress — агрегированный прогресс (для job.progress).
on_log      — сырые строки stdout/stderr (для полного лога задачи в UI).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mf_core.config import Settings, get_settings
from mf_core.errors import CancelledByUser, StallError, classify_ffmpeg
from mf_core.telemetry import FFMPEG_SPEED, get_logger

log = get_logger("mf.media")
ProgressCB = Callable[[dict[str, Any]], Awaitable[None] | None]
LogCB = Callable[[str, str], None]

def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _speed(v: str | None) -> float | None:
    if not v:
        return None
    return _f(v.strip().rstrip("x"))

@dataclass(slots=True)
class ProgressParser:
    """ffmpeg отдаёт out_time_us И out_time_ms — оба в микросекундах (историческая особенность)."""
    duration_s: float | None = None
    state: dict[str, str] = field(default_factory=dict)

    def feed(self, line: str) -> dict[str, Any] | None:
        k, _, v = line.strip().partition("=")
        if not k:
            return None
        self.state[k] = v
        if k != "progress":
            return None
        us = _f(self.state.get("out_time_us")) or _f(self.state.get("out_time_ms")) or 0.0
        cur = us / 1_000_000
        speed = _speed(self.state.get("speed"))
        pct = None
        eta = None
        if self.duration_s and self.duration_s > 0:
            pct = round(min(100.0, cur / self.duration_s * 100), 2)
            if speed and speed > 0:
                eta = round(max(0.0, (self.duration_s - cur) / speed), 1)
        return {
            "pct": pct, "out_time_s": round(cur, 3), "fps": _f(self.state.get("fps")),
            "speed": speed, "bitrate": self.state.get("bitrate"),
            "frame": int(_f(self.state.get("frame")) or 0),
            "total_size": int(_f(self.state.get("total_size")) or 0),
            "dup_frames": int(_f(self.state.get("dup_frames")) or 0),
            "drop_frames": int(_f(self.state.get("drop_frames")) or 0),
            "eta_s": eta, "done": v == "end",
        }

@dataclass(slots=True)
class RunResult:
    returncode: int
    elapsed_s: float
    stats: dict[str, Any]
    stderr_tail: list[str]
    avg_speed: float | None = None
    cancelled: bool = False

def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)

async def _graceful_stop(proc: asyncio.subprocess.Process, grace_s: int, stdin_q: bool) -> None:
    if proc.returncode is not None:
        return
    if stdin_q and proc.stdin is not None and not proc.stdin.is_closing():
        with contextlib.suppress(Exception):
            proc.stdin.write(b"q")
            await proc.stdin.drain()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), grace_s)
        return
    _signal_group(proc, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), 5)
        return
    _signal_group(proc, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await proc.wait()

async def run_media(
        cmd: list[str], *, duration_s: float | None = None,
        on_progress: ProgressCB | None = None, on_log: LogCB | None = None,
        cancel: asyncio.Event | None = None, stall_timeout_s: int | None = None,
        stdin_q: bool = False, settings: Settings | None = None, engine: str = "ffmpeg",
        codec: str = "unknown", progress_interval_s: float = 1.0, log_file: str | None = None,
) -> RunResult:
    s = settings or get_settings()
    stall = stall_timeout_s or s.stall_timeout_s
    parser = ProgressParser(duration_s)
    tail: deque[str] = deque(maxlen=200)
    started = time.monotonic()
    last_tick = started
    last_emit = 0.0
    last_snap: dict[str, Any] = {}
    speeds: list[float] = []
    written = 0
    fh = open(log_file, "w", encoding="utf-8") if log_file else None  # noqa: SIM115

    def emit_log(stream: str, line: str) -> None:
        if on_log is None:
            return
        try:
            on_log(stream, line)
        except Exception as e:  # noqa: BLE001
            log.warning("log_sink_failed", error=str(e))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin_q else asyncio.subprocess.DEVNULL,
        start_new_session=True, limit=1 << 20,
    )
    log.info("media_start", engine=engine, pid=proc.pid, duration_s=duration_s)

    async def emit(snap: dict[str, Any], force: bool = False) -> None:
        nonlocal last_emit, last_snap
        last_snap = snap
        if on_progress is None:
            return
        now = time.monotonic()
        if not force and now - last_emit < progress_interval_s:
            return
        last_emit = now
        res = on_progress(snap)
        if asyncio.iscoroutine(res):
            await res

    async def read_stdout() -> None:
        nonlocal last_tick
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            emit_log("stdout", line)
            snap = parser.feed(line)
            if snap is None:
                continue
            last_tick = time.monotonic()
            if snap.get("speed"):
                speeds.append(snap["speed"])
            await emit(snap, force=snap["done"])

    async def read_stderr() -> None:
        nonlocal last_tick, written
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            last_tick = time.monotonic()
            tail.append(line)
            emit_log("stderr", line)
            if fh:
                fh.write(line + "\n")
                written += 1
                if written % 64 == 0:
                    fh.flush()

    async def watchdog() -> None:
        while proc.returncode is None:
            await asyncio.sleep(1.0)
            if cancel is not None and cancel.is_set():
                await _graceful_stop(proc, s.ffmpeg_stop_grace_s, stdin_q)
                raise CancelledByUser("остановлено пользователем/воркером")
            idle = time.monotonic() - last_tick
            if idle > stall:
                _signal_group(proc, signal.SIGKILL)
                raise StallError(f"нет прогресса {idle:.0f} c", tail=list(tail)[-40:])

    try:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(read_stdout())
                tg.create_task(read_stderr())
                tg.create_task(watchdog())
        except* CancelledByUser as eg:
            raise eg.exceptions[0] from None
        except* StallError as eg:
            raise eg.exceptions[0] from None
    finally:
        if proc.returncode is None:
            await _graceful_stop(proc, 2, stdin_q)
        if fh:
            fh.flush()
            fh.close()

    rc = proc.returncode or 0
    elapsed = time.monotonic() - started
    avg = round(sum(speeds) / len(speeds), 3) if speeds else None
    if rc != 0:
        raise classify_ffmpeg(rc, list(tail))
    if avg:
        FFMPEG_SPEED.labels(engine, codec).observe(avg)
    log.info("media_done", engine=engine, rc=rc, elapsed_s=round(elapsed, 2), avg_speed=avg)
    return RunResult(rc, round(elapsed, 2), last_snap, list(tail)[-60:], avg)