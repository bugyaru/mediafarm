"""inotify-ускоритель для local/NFS. Только ускоряет: полный rescan всё равно обязателен."""
from __future__ import annotations

import asyncio
from pathlib import Path

from mf_core.telemetry import get_logger

log = get_logger("mf.notify")


class LocalNotifier:
    def __init__(self, path: Path, event: asyncio.Event, recursive: bool = True) -> None:
        self.path, self.event, self.recursive = path, event, recursive
        self._observer = None
        self._loop = asyncio.get_event_loop()

    def start(self) -> bool:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return False
        if not self.path.is_dir():
            return False

        outer = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:  # noqa: ANN001
                if getattr(event, "is_directory", False):
                    return
                try:
                    outer._loop.call_soon_threadsafe(outer.event.set)
                except RuntimeError:
                    pass

        try:
            self._observer = Observer()
            self._observer.schedule(Handler(), str(self.path), recursive=self.recursive)
            self._observer.daemon = True
            self._observer.start()
            log.info("inotify_started", path=str(self.path))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("inotify_unavailable", path=str(self.path), error=str(e))
            self._observer = None
            return False

    def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None