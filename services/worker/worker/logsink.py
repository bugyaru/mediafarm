"""Полный лог задачи: сырые строки инструментов → job_log (БД) + файлы в scratch.

put()/writer() вызываются СИНХРОННО из колбэков run_media/run_streaming — они только
кладут строку в буфер и в файл. Асинхронный флашер сбрасывает пачку в БД раз в
MF_LOG_FLUSH_INTERVAL_S (или когда набралось MF_LOG_FLUSH_LINES строк), после чего
триггер job_log шлёт NOTIFY job_logs и UI доливает «хвост» в детали задачи.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mf_core import joblog
from mf_core.config import Settings
from mf_core.telemetry import get_logger

log = get_logger("mf.joblog")

class LogSink:
    def __init__(
            self, *, job_id: uuid.UUID, attempt: int, session_factory, settings: Settings,
            workdir: Path, enabled: bool | None = None,
    ) -> None:
        self.job_id, self.attempt = job_id, attempt
        self.sf, self.s = session_factory, settings
        self.workdir = Path(workdir)
        self.enabled = settings.log_stream if enabled is None else enabled
        self.step_idx, self.step_name = -1, "job"

        self.max_lines = settings.log_max_lines_per_job
        self.flush_lines = max(1, settings.log_flush_lines)
        self.interval = max(0.1, settings.log_flush_interval_s)

        self._buf: list[dict[str, Any]] = []
        self._files: dict[tuple[int, str], Any] = {}
        self._wakeup = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._cap_noted = False
        self.total = 0
        self.dropped = 0
        self.failed = 0

    # ------------------------- синхронный API -------------------------
    def set_step(self, idx: int, name: str) -> None:
        self._close_files()
        self.step_idx, self.step_name = idx, name
        self.meta(f"── шаг {idx}: {name} ──")

    def writer(self, tool: str) -> Callable[[str, str], None]:
        def write(stream: str, line: str) -> None:
            self.put(tool, stream, line)
        return write

    def put(self, tool: str, stream: str, line: str) -> None:
        if not line or not line.strip():
            return
        kind, level, clean = joblog.normalize(
            tool, stream, line, max_chars=self.s.log_line_max_chars, redact=self.s.log_redact)
        self._to_file(tool, clean)
        if not self.enabled:
            return
        if kind == "progress" and not self.s.log_progress_lines:
            return
        self._append(tool, kind, level, clean)

    def meta(self, message: str, level: str = "info", tool: str = "worker",
             force: bool = False) -> None:
        clean = joblog.sanitize(message, max_chars=self.s.log_line_max_chars,
                                redact=self.s.log_redact)
        self._to_file(tool, clean)
        if self.enabled:
            self._append(tool, "meta", level, clean, force=force)

    def _append(self, tool: str, stream: str, level: str, line: str, *, force: bool = False) -> None:
        if self.total >= self.max_lines and not force:
            self.dropped += 1
            if not self._cap_noted:
                self._cap_noted = True
                self._buf.append(joblog.row(
                    self.job_id, step_idx=self.step_idx, attempt=self.attempt, tool="worker",
                    stream="meta", level="warning",
                    line=f"достигнут лимит {self.max_lines} строк — дальше лог только в файле"))
                self._wakeup.set()
            return
        self.total += 1
        self._buf.append(joblog.row(
            self.job_id, step_idx=self.step_idx, attempt=self.attempt, tool=tool,
            stream=stream, level=level, line=line))
        if len(self._buf) >= self.flush_lines:
            self._wakeup.set()

    # --------------------------- файлы -------------------------------
    def _to_file(self, tool: str, line: str) -> None:
        key = (self.step_idx, tool)
        fh = self._files.get(key)
        if fh is None:
            idx = self.step_idx if self.step_idx >= 0 else 0
            path = self.workdir / f"{idx:02d}_{tool}.log"
            try:
                fh = open(path, "a", encoding="utf-8")  # noqa: SIM115
            except OSError as e:
                log.warning("job_log_file_failed", path=str(path), error=str(e))
                return
            self._files[key] = fh
        with contextlib.suppress(OSError, ValueError):
            fh.write(line + "\n")

    def _flush_files(self) -> None:
        for fh in self._files.values():
            with contextlib.suppress(OSError, ValueError):
                fh.flush()

    def _close_files(self) -> None:
        for fh in self._files.values():
            with contextlib.suppress(OSError, ValueError):
                fh.flush()
                fh.close()
        self._files.clear()

    # ------------------------- асинхронный сброс ----------------------
    async def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"logsink-{self.job_id}")

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), self.interval)
            self._wakeup.clear()
            await self._flush()
            if self._stopping.is_set():
                break
        await self._flush()

    async def flush(self) -> None:
        await self._flush()

    async def _flush(self) -> None:
        self._flush_files()
        if not self._buf:
            return
        rows, self._buf = self._buf, []
        try:
            async with self.sf() as sess, sess.begin():
                await joblog.append(sess, rows)
        except Exception as e:  # noqa: BLE001
            self.failed += len(rows)
            log.warning("job_log_flush_failed", job_id=str(self.job_id),
                        lines=len(rows), error=str(e))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.dropped:
            self.meta(f"лог усечён: пропущено {self.dropped} строк "
                      f"(лимит MF_LOG_MAX_LINES_PER_JOB={self.max_lines})", "warning", force=True)
        self._stopping.set()
        self._wakeup.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=15)
            self._task = None
        await self._flush()
        self._close_files()