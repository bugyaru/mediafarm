"""LISTEN job_events / job_logs → fan-out по подписчикам (WebSocket). Без Redis."""
from __future__ import annotations

import asyncio
import contextlib

import asyncpg
import orjson

from mf_core.telemetry import get_logger

log = get_logger("mf.ui.bus")

class PgEventBus:
    """job_events — изменения задач (строки очереди), job_logs — новые строки логов."""

    CHANNELS = ("job_events", "job_logs")

    def __init__(self, dsn: str, queue_size: int = 64) -> None:
        self.dsn = dsn
        self.queue_size = queue_size
        self._subs: dict[str, set[asyncio.Queue]] = {c: set() for c in self.CHANNELS}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.connected = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="pg-event-bus")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for subs in self._subs.values():
            subs.clear()

    def subscribe(self, channel: str = "job_events") -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self._subs.setdefault(channel, set()).add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue, channel: str | None = None) -> None:
        if channel is None:
            for subs in self._subs.values():
                subs.discard(q)
        else:
            self._subs.get(channel, set()).discard(q)

    @property
    def subscribers(self) -> int:
        return sum(len(s) for s in self._subs.values())

    def subscribers_of(self, channel: str) -> int:
        return len(self._subs.get(channel, ()))

    def _listener(self, channel: str):
        def on_notify(_conn, _pid, _channel, payload: str) -> None:
            try:
                evt = orjson.loads(payload)
            except Exception:  # noqa: BLE001
                return
            for q in list(self._subs.get(channel, ())):
                if q.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()      # медленный клиент — теряем старые кадры
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(evt)
        return on_notify

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            conn = None
            try:
                conn = await asyncpg.connect(self.dsn)
                for channel in self.CHANNELS:
                    await conn.add_listener(channel, self._listener(channel))
                self.connected.set()
                backoff = 1.0
                log.info("event_bus_connected", subscribers=self.subscribers,
                         channels=list(self.CHANNELS))
                while not self._stopping.is_set():
                    await asyncio.sleep(15)
                    await conn.execute("SELECT 1")     # keepalive / детект обрыва
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.connected.clear()
                log.warning("event_bus_disconnected", error=str(e), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()