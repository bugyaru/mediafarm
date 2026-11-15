"""LISTEN job_events → fan-out по подписчикам (WebSocket). Без Redis."""
from __future__ import annotations

import asyncio
import contextlib

import asyncpg
import orjson

from mf_core.telemetry import get_logger

log = get_logger("mf.ui.bus")


class PgEventBus:
    def __init__(self, dsn: str, queue_size: int = 64) -> None:
        self.dsn = dsn
        self.queue_size = queue_size
        self._subs: set[asyncio.Queue] = set()
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
        self._subs.clear()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        try:
            evt = orjson.loads(payload)
        except Exception:  # noqa: BLE001
            return
        for q in list(self._subs):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()          # медленный клиент — теряем старые кадры
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(evt)

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            conn = None
            try:
                conn = await asyncpg.connect(self.dsn)
                await conn.add_listener("job_events", self._on_notify)
                self.connected.set()
                backoff = 1.0
                log.info("event_bus_connected", subscribers=self.subscribers)
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