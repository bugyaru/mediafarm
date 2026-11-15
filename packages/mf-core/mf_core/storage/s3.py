"""S3 / MinIO / Ceph RGW через s3fs (aiobotocore).

Публикация:
  * options.direct_publish=true (по умолчанию) — пишем сразу в финальный ключ:
    CompleteMultipartUpload атомарен, объект не виден до завершения.
  * иначе — tmp-ключ + server-side copy + delete (для бэкендов без атомарного MPU).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import unquote, urlsplit

import s3fs

from mf_core.errors import SourceInvalidError, StorageError
from mf_core.storage.base import Backend, Entry


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
        self._lock = asyncio.Lock()
        self.block = max(8, get_settings().storage_s3_multipart_mb) * 1024 * 1024
        self.direct = bool(self.options.get("direct_publish", True))

    async def fs(self) -> s3fs.S3FileSystem:
        async with self._lock:
            if self._fs is None:
                from mf_core.config import get_settings
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
            return self._fs

    def _key(self, path: str) -> str:
        rel = "/".join(p for p in (self.prefix, path.strip("/")) if p)
        return f"{self.bucket}/{rel}"

    def _rel(self, key: str) -> str:
        parts = key.split("/", 1)[1] if "/" in key else ""
        if self.prefix and parts.startswith(self.prefix):
            parts = parts[len(self.prefix):].lstrip("/")
        return parts

    def _mk(self, info: dict) -> Entry:
        key = info["name"]
        rel = self._rel(key)
        lm = info.get("LastModified") or info.get("mtime")
        mtime = lm.timestamp() if hasattr(lm, "timestamp") else float(lm or 0)
        etag = (info.get("ETag") or info.get("etag") or "").strip('"') or None
        return Entry(
            path=rel, uri=self.uri(rel), size=int(info.get("size") or 0),
            mtime=mtime, is_dir=info.get("type") == "directory", etag=etag,
            extra={"storage_class": info.get("StorageClass")},
        )

    async def stat(self, path: str) -> Entry:
        fs = await self.fs()
        try:
            info = await fs._info(self._key(path))
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
        fs = await self.fs()
        return bool(await fs._exists(self._key(path)))

    async def _reader(self, path: str, offset: int) -> AsyncIterator[bytes]:
        fs = await self.fs()
        async with await fs.open_async(self._key(path), "rb") as f:
            if offset:
                await f.seek(offset)
            while data := await f.read(self.chunk):
                yield data

    async def _writer(self, path: str, chunks: AsyncIterator[bytes], size: int | None) -> None:
        fs = await self.fs()
        async with await fs.open_async(self._key(path), "wb", block_size=self.block) as f:
            async for c in chunks:
                await f.write(c)

    async def _publish(self, tmp: str, final: str, overwrite: bool) -> None:
        fs = await self.fs()
        if tmp == final:
            fs.invalidate_cache(self._key(final))
            return
        if not overwrite and await fs._exists(self._key(final)):
            raise StorageError(f"цель существует: {final}")
        await fs._cp_file(self._key(tmp), self._key(final))
        await fs._rm_file(self._key(tmp))
        fs.invalidate_cache(self._key(final))

    def _tmp_name(self, final: str) -> str:
        # при direct_publish MPU сам по себе атомарен — tmp не нужен
        return final if self.direct else super()._tmp_name(final)

    async def delete(self, path: str) -> None:
        fs = await self.fs()
        try:
            await fs._rm_file(self._key(path))
        except FileNotFoundError:
            pass
        fs.invalidate_cache(self._key(path))

    async def mkdirs(self, path: str) -> None:
        return None  # в S3 нет директорий

    async def move(self, src: str, dst: str) -> None:
        self._guard_write()
        fs = await self.fs()
        await fs._mv(self._key(src), self._key(dst))
        fs.invalidate_cache()

    async def close(self) -> None:
        if self._fs is not None:
            try:
                await self._fs._close()
            except Exception:  # noqa: BLE001
                pass
            self._fs = None