"""S3-бэкенд (AWS S3 / MinIO / Ceph RGW) на базе s3fs.

Избегает проблем с async/sync mismatch в s3fs: не использует fs.open() и файловые
дескрипторы вообще. Вместо этого — атомарные методы:
  - _reader: fs.cat_file() → yield чанками из памяти
  - _writer: аккумулирует chunks в памяти/temp → fs.pipe_file() или fs.put_file()
  - _publish: fs._cp_file() + fs._rm_file() (rename в S3 нет)

Это гарантирует корректную работу с любой версией s3fs/fsspec.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator
from urllib.parse import unquote, urlsplit

import s3fs

from mf_core.config import get_settings
from mf_core.errors import SourceInvalidError, StorageError
from mf_core.storage.base import Backend, Entry


# Порог спулинга в память: файлы <= 64 MiB хранятся в bytearray, больше — в temp-файл.
_SPILL_LIMIT = 64 * 1024 * 1024


class S3Backend(Backend):
    kind = "s3"

    def __init__(self, row, creds=None):
        super().__init__(row, creds)
        u = urlsplit(row.base_uri)
        self.bucket = u.hostname or ""
        self.prefix = unquote(u.path).strip("/")
        if not self.bucket:
            raise StorageError(f"некорректный S3 URI: {row.base_uri}")

        self._fs: s3fs.S3FileSystem | None = None
        self._fs_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self.block = max(8, get_settings().storage_s3_multipart_mb) * 1024 * 1024

    # ── нормализация путей ────────────────────────────────────────────────
    def _key(self, path: str) -> str:
        p = str(path)
        if "://" in p:
            p = unquote(urlsplit(p).path)
        elif self.bucket and p.startswith(self.bucket + "/"):
            p = p[len(self.bucket) + 1:]
        p = p.lstrip("/")
        if self.prefix:
            if p == self.prefix:
                p = ""
            elif p.startswith(self.prefix + "/"):
                p = p[len(self.prefix) + 1:]
        rel = "/".join(x for x in (self.prefix, p.strip("/")) if x)
        return f"{self.bucket}/{rel}"

    def _rel(self, key: str) -> str:
        parts = key.split("/", 1)[1] if "/" in key else ""
        if self.prefix and parts.startswith(self.prefix):
            parts = parts[len(self.prefix):].lstrip("/")
        return parts

    def _mk(self, info: dict) -> Entry:
        rel = self._rel(info["name"])
        lm = info.get("LastModified") or info.get("mtime")
        mtime = lm.timestamp() if hasattr(lm, "timestamp") else float(lm or 0)
        etag = (info.get("ETag") or info.get("etag") or "").strip('"') or None
        return Entry(
            path=rel, uri=self.uri(rel), size=int(info.get("size") or 0),
            mtime=mtime, is_dir=info.get("type") == "directory", etag=etag,
            extra={"storage_class": info.get("StorageClass")},
        )

    # ── управление соединением (lifecycle) ────────────────────────────────
    @staticmethod
    def _fs_dead(fs: s3fs.S3FileSystem | None) -> bool:
        if fs is None:
            return True
        return bool(getattr(fs, "_closed", False) or getattr(fs, "closed", False))

    async def _close_fs(self) -> None:
        fs, self._fs, self._fs_loop = self._fs, None, None
        if fs is not None:
            try:
                # fs.close() в s3fs — корутина при asynchronous=True
                res = fs.close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        async with self._lock:
            await self._close_fs()

    async def fs(self) -> s3fs.S3FileSystem:
        async with self._lock:
            loop = asyncio.get_running_loop()
            if self._fs is not None and (self._fs_dead(self._fs) or self._fs_loop is not loop):
                await self._close_fs()

            if self._fs is None:
                client_kwargs: dict = {}
                if self.options.get("endpoint_url"):
                    client_kwargs["endpoint_url"] = self.options["endpoint_url"]
                if self.options.get("region"):
                    client_kwargs["region_name"] = self.options["region"]

                self._fs = s3fs.S3FileSystem(
                    key=self.creds.get("access_key"),
                    secret=self.creds.get("secret_key"),
                    token=self.creds.get("session_token"),
                    asynchronous=True,
                    client_kwargs=client_kwargs,
                    config_kwargs={
                        "max_pool_connections": get_settings().storage_s3_concurrency,
                        "retries": {"max_attempts": 5, "mode": "standard"},
                        "s3": {"addressing_style": self.options.get("addressing_style", "path")},
                    },
                )
                self._fs_loop = loop
            return self._fs

    async def _with_fs(self, op):
        """Выполняет операцию с fs, автоматически пересоздавая его при закрытии."""
        fs = await self.fs()
        try:
            return await op(fs)
        except ValueError as e:
            if "closed file" not in str(e).lower() and "I/O operation" not in str(e):
                raise
            async with self._lock:
                await self._close_fs()
            fs = await self.fs()
            return await op(fs)

    # ── примитивы контракта Backend ───────────────────────────────────────
    async def stat(self, path: str) -> Entry:
        try:
            info = await self._with_fs(lambda fs: fs._info(self._key(path)))
        except FileNotFoundError as e:
            raise SourceInvalidError(f"не найдено: {self.uri(path)}") from e
        return self._mk(info)

    async def list(self, path: str = "", recursive: bool = False) -> AsyncIterator[Entry]:
        fs = await self.fs()
        stack = [self._key(path)]
        while stack:
            cur = stack.pop()
            try:
                items = await fs._ls(cur, detail=True)
            except FileNotFoundError:
                continue
            for info in items:
                if info.get("type") == "directory":
                    if recursive:
                        stack.append(info["name"])
                    continue
                yield self._mk(info)

    async def exists(self, path: str) -> bool:
        return bool(await self._with_fs(lambda fs: fs._exists(self._key(path))))

    async def _reader(self, path: str, offset: int) -> AsyncIterator[bytes]:
        """Чтение через fs.cat_file() — атомарно, без открытия дескриптора."""
        key = self._key(path)
        fs = await self.fs()
        try:
            # cat_file возвращает bytes целиком; для больших файлов это неоптимально,
            # но гарантирует корректность. При необходимости можно разбить на range-запросы.
            data = await fs._cat_file(key, start=offset or None)
        except FileNotFoundError as e:
            raise SourceInvalidError(f"не найдено: {self.uri(path)}") from e

        # Yield чанками по self.chunk
        for i in range(0, len(data), self.chunk):
            yield data[i:i + self.chunk]

    async def _writer(self, path: str, chunks: AsyncIterator[bytes], size: int | None) -> None:
        """Запись через спулинг в память/temp + fs.pipe_file() или fs.put_file()."""
        key = self._key(path)
        fs = await self.fs()

        # Аккумулируем chunks
        buf = bytearray()
        tmp_file = None
        spilled = False

        async for data in chunks:
            if spilled:
                tmp_file.write(data)
            else:
                buf.extend(data)
                if len(buf) > _SPILL_LIMIT:
                    # Переполнение: спулим в temp-файл
                    tmp_file = tempfile.NamedTemporaryFile(prefix="mf-s3-", suffix=".part", delete=False)
                    tmp_file.write(buf)
                    buf = bytearray()
                    spilled = True

        try:
            if spilled:
                # Большой файл: загружаем с диска через put_file (multipart)
                tmp_file.flush()
                tmp_file.close()
                await fs._put_file(tmp_file.name, key)
            else:
                # Маленький файл: загружаем из памяти через pipe_file (single PUT)
                await fs._pipe_file(key, bytes(buf))

            fs.invalidate_cache(key)
        finally:
            if tmp_file is not None:
                try:
                    os.unlink(tmp_file.name)
                except OSError:
                    pass

    async def _publish(self, tmp: str, final: str, overwrite: bool) -> None:
        """В S3 нет rename, поэтому используем copy + delete."""
        tkey, fkey = self._key(tmp), self._key(final)

        async def op(fs):
            if tkey == fkey:
                fs.invalidate_cache(fkey)
                return
            if not overwrite and await fs._exists(fkey):
                raise StorageError(f"цель существует: {final}")

            if not await fs._exists(tkey):
                raise StorageError(
                    f"временный объект не найден: {self.uri(tmp)} "
                    f"(возможно, сбой сети или прав доступа при записи)"
                )

            await fs._cp_file(tkey, fkey)
            await fs._rm_file(tkey)
            fs.invalidate_cache(fkey)
            fs.invalidate_cache(tkey)

        await self._with_fs(op)

    async def delete(self, path: str) -> None:
        key = self._key(path)
        async def op(fs):
            try:
                await fs._rm_file(key)
            except FileNotFoundError:
                pass
            fs.invalidate_cache(key)
        await self._with_fs(op)

    async def mkdirs(self, path: str) -> None:
        # S3 — плоское пространство имён, префиксы создаются неявно.
        return None

    async def move(self, src: str, dst: str) -> None:
        self._guard_write()
        await self._publish(src, dst, overwrite=True)