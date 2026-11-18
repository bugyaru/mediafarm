"""NFS-бэкенд через libnfs (Python-биндинги к C-библиотеке libnfs).

URL формат: nfs://server/export/path
Требует: pip install libnfs
"""
from __future__ import annotations

import asyncio
import os
import stat as statmod
import threading
from collections.abc import AsyncIterator
from urllib.parse import unquote, urlsplit

try:
    from libnfs import NFS
except ImportError:
    NFS = None

from mf_core.config import get_settings
from mf_core.errors import SourceInvalidError, StorageError
from mf_core.storage.base import Backend, Entry

# Кэш NFS-клиентов по URL
_CLIENTS: dict[str, NFS] = {}
_LOCK = threading.Lock()


def _get_nfs_client(url: str) -> NFS:
    """Получить или создать NFS-клиент для URL."""
    with _LOCK:
        if url not in _CLIENTS:
            if NFS is None:
                raise StorageError(
                    "libnfs не установлен. Установите: pip install libnfs"
                )
            _CLIENTS[url] = NFS(url)
        return _CLIENTS[url]


class NfsBackend(Backend):
    kind = "nfs"

    def __init__(self, row, creds=None):
        super().__init__(row, creds)
        u = urlsplit(row.base_uri)
        self.host = u.hostname or ""
        self.port = int(u.port or self.options.get("port", 2049))
        parts = unquote(u.path).strip("/").split("/", 1)
        self.share = parts[0]
        self.prefix = parts[1] if len(parts) > 1 else ""
        if not self.host or not self.share:
            raise StorageError(f"некорректный NFS URI: {row.base_uri}")

        # Формируем базовый URL для libnfs: nfs://host/share
        self._nfs_url = f"nfs://{self.host}/{self.share}"
        self._client: NFS | None = None

    def _client(self) -> NFS:
        if self._client is None:
            self._client = _get_nfs_client(self._nfs_url)
        return self._client

    def _path(self, path: str) -> str:
        """Относительный путь внутри NFS-экспорта."""
        return "/".join(p for p in (self.prefix, path.strip("/")) if p)

    def _rel(self, full_path: str) -> str:
        """Из полного пути внутри экспорта — относительный путь."""
        if self.prefix and full_path.startswith(self.prefix + "/"):
            return full_path[len(self.prefix) + 1:]
        return full_path

    def _mk(self, full_path: str, st) -> Entry:
        rel = self._rel(full_path)
        return Entry(
            path=rel, uri=self.uri(rel), size=int(st.st_size),
            mtime=float(st.st_mtime), is_dir=statmod.S_ISDIR(st.st_mode),
            extra={"inode": st.st_ino, "mode": st.st_mode},
        )

    async def stat(self, path: str) -> Entry:
        def _do():
            client = self._client()
            return client.stat(self._path(path))
        try:
            st = await asyncio.to_thread(_do)
        except FileNotFoundError as e:
            raise SourceInvalidError(f"не найдено: {self.uri(path)}") from e
        except Exception as e:
            raise StorageError(f"NFS stat {path}: {e}") from e
        return self._mk(self._path(path), st)

    async def list(self, path: str = "", recursive: bool = False) -> AsyncIterator[Entry]:
        def _scan() -> list[Entry]:
            client = self._client()
            out: list[Entry] = []
            stack = [self._path(path)]
            while stack:
                cur = stack.pop()
                try:
                    dir_handle = client.opendir(cur)
                    try:
                        for entry in client.readdir(dir_handle):
                            name = entry.name
                            if name in (".", ".."):
                                continue
                            full = f"{cur}/{name}" if cur else name
                            st = client.stat(full)
                            if statmod.S_ISDIR(st.st_mode):
                                if recursive:
                                    stack.append(full)
                                continue
                            out.append(self._mk(full, st))
                    finally:
                        client.closedir(dir_handle)
                except Exception:
                    continue
            return out
        for e in await asyncio.to_thread(_scan):
            yield e

    async def exists(self, path: str) -> bool:
        def _do():
            client = self._client()
            try:
                client.stat(self._path(path))
                return True
            except FileNotFoundError:
                return False
        return bool(await asyncio.to_thread(_do))

    async def _reader(self, path: str, offset: int) -> AsyncIterator[bytes]:
        def _open():
            client = self._client()
            fd = client.open(self._path(path), os.O_RDONLY)
            if offset:
                client.lseek(fd, offset, os.SEEK_SET)
            return fd
        fd = await asyncio.to_thread(_open)
        try:
            while True:
                data = await asyncio.to_thread(self._client().read, fd, self.chunk)
                if not data:
                    break
                yield data
        finally:
            await asyncio.to_thread(self._client().close, fd)

    async def _writer(self, path: str, chunks: AsyncIterator[bytes], size: int | None) -> None:
        def _open():
            client = self._client()
            # O_WRONLY | O_CREAT | O_TRUNC
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            return client.open(self._path(path), flags)
        fd = await asyncio.to_thread(_open)
        try:
            async for c in chunks:
                await asyncio.to_thread(self._client().write, fd, c)
            await asyncio.to_thread(self._client().fsync, fd)
        finally:
            await asyncio.to_thread(self._client().close, fd)

    async def _publish(self, tmp: str, final: str, overwrite: bool) -> None:
        def _do():
            client = self._client()
            src, dst = self._path(tmp), self._path(final)
            if overwrite:
                try:
                    client.unlink(dst)
                except FileNotFoundError:
                    pass
            client.rename(src, dst)
        await asyncio.to_thread(_do)

    async def delete(self, path: str) -> None:
        def _do():
            client = self._client()
            p = self._path(path)
            st = client.stat(p)
            if statmod.S_ISDIR(st.st_mode):
                client.rmdir(p)
            else:
                client.unlink(p)
        await asyncio.to_thread(_do)

    async def mkdirs(self, path: str) -> None:
        def _do():
            client = self._client()
            # libnfs не имеет makedirs, создаём рекурсивно
            parts = self._path(path).split("/")
            cur = ""
            for part in parts:
                cur = f"{cur}/{part}" if cur else part
                try:
                    client.stat(cur)
                except FileNotFoundError:
                    client.mkdir(cur)
        await asyncio.to_thread(_do)

    async def move(self, src: str, dst: str) -> None:
        self._guard_write()
        def _do():
            client = self._client()
            # Создаём родительскую директорию dst
            dst_path = self._path(dst)
            parent = "/".join(dst_path.split("/")[:-1])
            if parent:
                parts = parent.split("/")
                cur = ""
                for part in parts:
                    cur = f"{cur}/{part}" if cur else part
                    try:
                        client.stat(cur)
                    except FileNotFoundError:
                        client.mkdir(cur)
            client.rename(self._path(src), dst_path)
        await asyncio.to_thread(_do)

    async def close(self) -> None:
        # libnfs не имеет явного close для клиента, просто удаляем из кэша
        with _LOCK:
            _CLIENTS.pop(self._nfs_url, None)
        self._client = None