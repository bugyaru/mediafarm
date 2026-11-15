"""Единый интерфейс хранилищ.

Backend реализует 6 примитивов: stat/list/exists/_reader/_writer/_publish (+delete/move/mkdirs).
Резюмирование докачки, прогресс, отмена, проверка размера и атомарная публикация — общие.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import aiofiles

from mf_core.config import get_settings
from mf_core.errors import CancelledByUser, PublishConflictError, StorageError

ProgressCB = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class StorageRow:
    """Лёгкая копия ORM Storage — чтобы backend'ы не тянули сессию."""
    id: uuid.UUID
    name: str
    kind: str
    base_uri: str
    mount_path: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    secret_ref: str | None = None
    read_only: bool = False
    enabled: bool = True

    @classmethod
    def from_orm(cls, obj: Any) -> StorageRow:
        return cls(
            id=obj.id, name=obj.name, kind=obj.kind, base_uri=obj.base_uri,
            mount_path=obj.mount_path, options=dict(obj.options or {}),
            secret_ref=obj.secret_ref, read_only=obj.read_only, enabled=obj.enabled,
        )


@dataclass(frozen=True, slots=True)
class Entry:
    path: str                      # путь относительно base_uri хранилища
    uri: str                       # полный URI
    size: int
    mtime: float                   # epoch seconds
    is_dir: bool = False
    etag: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.rstrip("/").rsplit("/", 1)[-1]

    @property
    def fingerprint(self) -> str:
        return self.etag or f"{self.size}:{int(self.mtime)}"


@dataclass(slots=True)
class TransferResult:
    path: str
    size: int
    resumed_from: int = 0
    seconds: float = 0.0
    checksum: str | None = None


class Reporter:
    """Троттлинг прогресса: не чаще раза в interval, плюс финальный вызов."""

    def __init__(self, cb: ProgressCB | None, interval: float = 0.5, **base: Any) -> None:
        self.cb, self.interval, self.base = cb, interval, base
        self._last = 0.0

    async def __call__(self, done: int, total: int | None, force: bool = False, **kw: Any) -> None:
        if self.cb is None:
            return
        now = time.monotonic()
        if not force and now - self._last < self.interval:
            return
        self._last = now
        payload = {
            "bytes": done, "total": total,
            "pct": round(done / total * 100, 2) if total else None,
            **self.base, **kw,
        }
        res = self.cb(payload)
        if asyncio.iscoroutine(res):
            await res


def _check_cancel(cancel: asyncio.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledByUser("передача отменена")


class Backend(ABC):
    kind: ClassVar[str] = "abstract"

    def __init__(self, row: StorageRow, creds: dict[str, str] | None = None) -> None:
        self.row = row
        self.creds = creds or {}
        self.options = row.options or {}
        s = get_settings()
        self.chunk = max(1, s.storage_chunk_mb) * 1024 * 1024
        self.tmp_suffix = s.storage_tmp_suffix

    # ---- обязательные примитивы ----
    @abstractmethod
    async def stat(self, path: str) -> Entry: ...
    @abstractmethod
    def list(self, path: str = "", recursive: bool = False) -> AsyncIterator[Entry]: ...
    @abstractmethod
    async def exists(self, path: str) -> bool: ...
    @abstractmethod
    def _reader(self, path: str, offset: int) -> AsyncIterator[bytes]: ...
    @abstractmethod
    async def _writer(self, path: str, chunks: AsyncIterator[bytes], size: int | None) -> None: ...
    @abstractmethod
    async def _publish(self, tmp: str, final: str, overwrite: bool) -> None: ...
    @abstractmethod
    async def delete(self, path: str) -> None: ...
    @abstractmethod
    async def mkdirs(self, path: str) -> None: ...

    async def move(self, src: str, dst: str) -> None:
        raise StorageError(f"{self.kind}: move не поддержан")

    async def close(self) -> None:
        return None

    # ---- общие операции ----
    def uri(self, path: str) -> str:
        return f"{self.row.base_uri.rstrip('/')}/{path.lstrip('/')}"

    def _tmp_name(self, final: str) -> str:
        return f"{final}{self.tmp_suffix}-{uuid.uuid4().hex[:12]}"

    async def read_range(self, path: str, offset: int = 0, length: int | None = None) -> bytes:
        out = bytearray()
        async for chunk in self._reader(path, offset):
            out += chunk
            if length is not None and len(out) >= length:
                return bytes(out[:length])
        return bytes(out)

    async def checksum(self, path: str, algo: str = "sha256") -> str:
        h = hashlib.new(algo)
        async for chunk in self._reader(path, 0):
            h.update(chunk)
        return h.hexdigest()

    async def free_space(self) -> int | None:
        return None

    def _guard_write(self) -> None:
        if self.row.read_only:
            raise StorageError(f"хранилище {self.row.name} помечено read_only")

    async def download(
            self, path: str, dst: str | Path, *, resume: bool = True,
            on_progress: ProgressCB | None = None, cancel: asyncio.Event | None = None,
    ) -> TransferResult:
        """Скачивание с докачкой. Пишем в <dst>.part, по завершении — атомарный rename."""
        started = time.monotonic()
        entry = await self.stat(path)
        if entry.is_dir:
            raise StorageError(f"{entry.uri}: это директория")
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = Path(str(dst) + self.tmp_suffix)

        offset = 0
        if resume and part.exists():
            offset = part.stat().st_size
            if offset > entry.size:          # источник изменился/уменьшился — начинаем заново
                offset = 0
        if offset and offset == entry.size:  # уже скачано целиком
            os.replace(part, dst)
            return TransferResult(str(dst), entry.size, offset, time.monotonic() - started)

        rep = Reporter(on_progress, direction="download", storage=self.row.name, path=path)
        mode = "ab" if offset else "wb"
        async with aiofiles.open(part, mode) as f:
            if offset:
                await f.seek(offset)
            async for chunk in self._reader(path, offset):
                _check_cancel(cancel)
                await f.write(chunk)
                offset += len(chunk)
                await rep(offset, entry.size)
            await f.flush()
            os.fsync(f.fileno())

        if offset != entry.size:
            raise StorageError(
                f"размер не совпал для {entry.uri}: получено {offset}, ожидалось {entry.size}"
            )
        os.replace(part, dst)
        await rep(offset, entry.size, force=True)
        return TransferResult(str(dst), entry.size, offset, time.monotonic() - started)

    async def upload(
            self, src: str | Path, path: str, *, overwrite: bool = False,
            on_progress: ProgressCB | None = None, cancel: asyncio.Event | None = None,
            checksum: str | None = None,
    ) -> Entry:
        """Атомарная публикация: tmp → publish.半成品 никогда не виден под финальным именем."""
        self._guard_write()
        src = Path(src)
        if not src.is_file():
            raise StorageError(f"нет файла для выгрузки: {src}")
        size = src.stat().st_size
        if not overwrite and await self.exists(path):
            raise PublishConflictError(f"{self.uri(path)} уже существует", path=path)

        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent:
            await self.mkdirs(parent)

        tmp = self._tmp_name(path)
        rep = Reporter(on_progress, direction="upload", storage=self.row.name, path=path)
        done = 0

        async def chunks() -> AsyncIterator[bytes]:
            nonlocal done
            async with aiofiles.open(src, "rb") as f:
                while data := await f.read(self.chunk):
                    _check_cancel(cancel)
                    done += len(data)
                    await rep(done, size)
                    yield data

        try:
            await self._writer(tmp, chunks(), size)
            await self._publish(tmp, path, overwrite)
        except BaseException:
            try:
                await self.delete(tmp)
            except Exception:  # noqa: BLE001
                pass
            raise
        await rep(size, size, force=True)
        entry = await self.stat(path)
        if entry.size != size:
            raise StorageError(f"после выгрузки размер не совпал: {entry.size} != {size}")
        if checksum:
            actual = await self.checksum(path)
            if actual != checksum:
                raise StorageError(f"checksum не совпал для {entry.uri}")
        return entry

    async def write_bytes(self, path: str, data: bytes, *, overwrite: bool = False) -> Entry:
        self._guard_write()
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent:
            await self.mkdirs(parent)
        tmp = self._tmp_name(path)

        async def one() -> AsyncIterator[bytes]:
            yield data

        await self._writer(tmp, one(), len(data))
        await self._publish(tmp, path, overwrite)
        return await self.stat(path)