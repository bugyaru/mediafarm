"""Универсальный запуск внешнего процесса: построчный stdout, watchdog, отмена.

Используется yt-dlp и другими не-ffmpeg инструментами. run_media останется
ffmpeg-специфичным; общий рефакторинг.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mf_core.config import Settings, get_settings
from mf_core.errors import CancelledByUser, StallError
from mf_core.telemetry import get_logger

log = get_logger("mf.proc")
LineCB = Callable[[str], Awaitable[None] | None]


@dataclass(slots=True)
class ProcResult:
    returncode: int
    elapsed_s: float
    lines: int
    stderr_tail: list[str]


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)


async def _stop(proc: asyncio.subprocess.Process, grace_s: int, stdin_q: bool) -> None:
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


async def run_streaming(
        cmd: list[str], *, on_line: LineCB | None = None, cancel: asyncio.Event | None = None,
        stall_timeout_s: int | None = None, stdin_q: bool = False,
        settings: Settings | None = None, log_file: str | None = None,
) -> ProcResult:
    s = settings or get_settings()
    stall = stall_timeout_s or s.stall_timeout_s
    tail: deque[str] = deque(maxlen=200)
    started = last_tick = time.monotonic()
    count = 0
    fh = open(log_file, "w", encoding="utf-8") if log_file else None  # noqa: SIM115

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin_q else asyncio.subprocess.DEVNULL,
        start_new_session=True, limit=1 << 20,
    )

    async def read_stdout() -> None:
        nonlocal last_tick, count
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            last_tick = time.monotonic()
            count += 1
            if on_line is not None:
                res = on_line(line)
                if asyncio.iscoroutine(res):
                    await res

    async def read_stderr() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            tail.append(line)
            if fh:
                fh.write(line + "\n")

    async def watchdog() -> None:
        while proc.returncode is None:
            await asyncio.sleep(1.0)
            if cancel is not None and cancel.is_set():
                await _stop(proc, s.ffmpeg_stop_grace_s, stdin_q)
                raise CancelledByUser("процесс остановлен")
            idle = time.monotonic() - last_tick
            if idle > stall:
                _signal_group(proc, signal.SIGKILL)
                raise StallError(f"нет вывода {idle:.0f} c", tail=list(tail)[-40:])

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
            await _stop(proc, 2, stdin_q)
        if fh:
            fh.close()

    return ProcResult(proc.returncode or 0, round(time.monotonic() - started, 2),
                      count, list(tail)[-60:])