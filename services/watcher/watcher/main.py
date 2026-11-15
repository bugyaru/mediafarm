from __future__ import annotations

import asyncio
import contextlib
import signal
import time
import uuid
import zlib
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mf_core import db
from mf_core.config import Settings, get_settings
from mf_core.storage import StorageRegistry
from mf_core.storage.local import LocalBackend
from mf_core.telemetry import HealthState, get_logger, serve_observability, setup_logging
from watcher import metrics as M
from watcher.archiver import archive_pass
from watcher.emitter import Emitter
from watcher.notify import LocalNotifier
from watcher.scanner import FolderScanner

log = get_logger("mf.watcher")

SELECT_FOLDERS = text("""
SELECT w.id, w.name, w.storage_id, w.path, w.recursive, w.enabled, w.poll_interval_s,
       w.stability_s, w.include, w.exclude, w.ready_marker, w.parser_id, w.parser_opts,
       w.preset_id, w.job_template, w.concurrency_key, w.max_concurrency,
       w.archive_mode, w.archive_path, s.base_uri AS storage_base_uri
FROM watch_folder w JOIN storage s ON s.id = w.storage_id
WHERE w.enabled ORDER BY w.name
""")
SELECT_FOLDER = text("""
SELECT w.*, s.base_uri AS storage_base_uri
FROM watch_folder w JOIN storage s ON s.id = w.storage_id
WHERE w.id = :i AND w.enabled
""")


def folder_lock_key(settings: Settings, folder_id: uuid.UUID) -> int:
    return settings.watcher_lock_base + (zlib.crc32(str(folder_id).encode()) % 1_000_000)


class Watcher:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.stopping = asyncio.Event()
        self.state = HealthState(healthy=True, ready=False)
        self.registry = StorageRegistry()
        self.sf = None
        self.lock_engine = None
        self.owned: set[uuid.UUID] = set()
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}
        self._retry_at: dict[uuid.UUID, float] = {}
        self._scanners: dict[uuid.UUID, FolderScanner] = {}
        self._server = None

    def request_stop(self, reason: str = "sigterm") -> None:
        if self.stopping.is_set():
            return
        log.info("watcher_stopping", reason=reason)
        self.stopping.set()
        self.state.ready = False
        for t in self._tasks.values():
            t.cancel()

    async def run(self) -> None:
        setup_logging()
        s = self.s
        self._server = await serve_observability(s.metrics_port, self.state)
        self.sf = async_sessionmaker(db.get_engine(), expire_on_commit=False, autoflush=False)
        # отдельный engine под advisory-локи: соединение держится всё время владения
        self.lock_engine = create_async_engine(s.db_dsn, poolclass=NullPool)
        async with self.sf() as sess, sess.begin():
            await self.registry.refresh(sess)
        self._install_signals()
        self.state.ready = True
        log.info("watcher_started", metrics_port=s.metrics_port)
        try:
            while not self.stopping.is_set():
                try:
                    await self._reconcile()
                except Exception as e:  # noqa: BLE001
                    log.warning("reconcile_failed", error=str(e))
                await asyncio.sleep(s.watcher_refresh_s)
        finally:
            await self._shutdown()

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.request_stop, sig.name)

    async def _reconcile(self) -> None:
        async with self.sf() as sess, sess.begin():
            await self.registry.ensure_fresh(sess)
            rows = (await sess.execute(SELECT_FOLDERS)).mappings().all()
        wanted = {r["id"]: dict(r) for r in rows}
        for fid in list(self._tasks):
            if fid not in wanted or self._tasks[fid].done():
                task = self._tasks.pop(fid, None)
                if task and not task.done():
                    task.cancel()
        now = time.monotonic()
        for fid, row in wanted.items():
            if fid in self._tasks and not self._tasks[fid].done():
                continue
            if self._retry_at.get(fid, 0) > now:
                continue
            self._tasks[fid] = asyncio.create_task(self._own_folder(fid, row),
                                                   name=f"folder-{row['name']}")
        M.OWNED.set(len(self.owned))
        self.state.detail.update({"owned": len(self.owned), "folders": len(wanted)})

    async def _own_folder(self, fid: uuid.UUID, row: dict) -> None:
        from mf_core.queue import advisory_lock

        key = folder_lock_key(self.s, fid)
        try:
            async with advisory_lock(self.lock_engine, key) as got:
                if not got:
                    self._retry_at[fid] = time.monotonic() + self.s.watcher_lock_retry_s
                    log.debug("folder_busy", folder=row["name"])
                    return
                self.owned.add(fid)
                M.OWNED.set(len(self.owned))
                log.info("folder_acquired", folder=row["name"])
                try:
                    await self._folder_loop(fid, row)
                finally:
                    self.owned.discard(fid)
                    self._scanners.pop(fid, None)
                    M.OWNED.set(len(self.owned))
                    log.info("folder_released", folder=row["name"])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("folder_owner_failed", folder=row["name"], error=str(e))
            self._retry_at[fid] = time.monotonic() + self.s.watcher_error_backoff_s

    async def _folder_loop(self, fid: uuid.UUID, row: dict) -> None:
        s = self.s
        event = asyncio.Event()
        notifier: LocalNotifier | None = None
        last_archive = 0.0
        try:
            while not self.stopping.is_set():
                async with self.sf() as sess:
                    fresh = (await sess.execute(SELECT_FOLDER, {"i": fid})).mappings().first()
                if fresh is None:
                    return
                wf = dict(fresh)
                try:
                    be, _rel = self.registry.resolve(_folder_uri(wf))
                except Exception as e:  # noqa: BLE001
                    log.warning("folder_storage_unresolved", folder=wf["name"], error=str(e))
                    await asyncio.sleep(s.watcher_error_backoff_s)
                    continue

                scanner = self._scanners.get(fid)
                if scanner is None or scanner.wf["stability_s"] != wf["stability_s"]:
                    scanner = FolderScanner(wf, be, Emitter(self.registry, s), self.sf, s,
                                            self.registry)
                    self._scanners[fid] = scanner
                else:
                    scanner.wf, scanner.be = wf, be

                if notifier is None and s.watcher_inotify and isinstance(be, LocalBackend):
                    notifier = LocalNotifier(Path(be.root) / wf["path"].lstrip("/"),
                                             event, wf["recursive"])
                    notifier.start()

                try:
                    if wf.get("scan_requested_at"):
                        async with self.sf() as sess, sess.begin():
                            await sess.execute(text(
                                "UPDATE watch_folder SET scan_requested_at=NULL WHERE id=:i"),
                                {"i": fid})
                        event.set()
                    await scanner.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("scan_tick_failed", folder=wf["name"], error=str(e))
                    await asyncio.sleep(s.watcher_error_backoff_s)
                    continue

                now = time.monotonic()
                if now - last_archive >= s.watcher_archive_interval_s:
                    last_archive = now
                    with contextlib.suppress(Exception):
                        await archive_pass(wf, self.registry, self.sf, s)

                event.clear()
                interval = max(5, int(wf["poll_interval_s"]))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), timeout=interval)
        finally:
            if notifier is not None:
                notifier.stop()

    async def _shutdown(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.registry.close()
        if self.lock_engine is not None:
            await self.lock_engine.dispose()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        await db.dispose()
        log.info("watcher_stopped")


def _folder_uri(wf: dict) -> str:
    base = (wf.get("storage_base_uri") or "").rstrip("/")
    path = (wf.get("path") or "").lstrip("/")
    return f"{base}/{path}" if path else base


def cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="mf-watcher")
    parser.add_argument("--once", action="store_true", help="один проход по всем папкам")
    args = parser.parse_args()
    w = Watcher()
    if args.once:
        w.s.watcher_refresh_s = 1
    asyncio.run(w.run())


if __name__ == "__main__":
    cli()