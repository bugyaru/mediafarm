"""FTP/FTPS через aioftp. Соединение на операцию + семафор против перегрузки сервера."""
from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlsplit

import aioftp

from mf_core.config import get_settings
from mf_core.errors import SourceInvalidError, StorageError
from mf_core.storage.base import Backend, Entry


def _parse_modify(value: str | None) -> float:
    if not value:
        return 0.0
    v = value.split(".")[0]
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M"):
        try:
            return dt.datetime.strptime(v, fmt).replace(tzinfo=dt.UTC).timestamp()
        except ValueError:
            continue
    return 0.0


class FtpBackend(Backend):
    kind = "ftp"

    def __init__(self, row, creds=None):
        super().__init__(row, creds)
        u = urlsplit(row.base_uri)
        self.host = u.hostname or ""
        self.port = int(u.port or 21)
        self.prefix = unquote(u.path).strip("/")
        self.tls = bool(self.options.get("tls", False))
        self._sem = asyncio.Semaphore(get_settings().storage_ftp_max_sessions)

    @asynccontextmanager
    async def _conn(self):
        async with self._sem:
            client = aioftp.Client(
                socket_timeout=get_settings().storage_smb_timeout_s,
                path_timeout=get_settings().storage_smb_timeout_s,
            )
            try:
                await client.connect(self.host, self.port)
                if self.tls:
                    await client.starttls()
                await client.login(
                    self.creds.get("username") or "anonymous",
                    self.creds.get("password") or "anonymous@",
                    )
                yield client
            finally:
                try:
                    await client.quit()
                except Exception:  # noqa: BLE001
                    pass

    def _path(self, path: str) -> str:
        return "/" + "/".join(p for p in (self.prefix, path.strip("/")) if p)

    def _rel(self, p) -> str:
        s = str(p).lstrip("/")
        if self.prefix and s.startswith(self.prefix):
            s = s[len(self.prefix):].lstrip("/")
        return s

    def _mk(self, p, info: dict) -> Entry:
        rel = self._rel(p)
        return Entry(
            path=rel, uri=self.uri(rel), size=int(info.get("size") or 0),
            mtime=_parse_modify(info.get("modify")),
            is_dir=info.get("type") in ("dir", "cdir", "pdir"),
        )

    async def stat(self, path: str) -> Entry:
        async with self._conn() as c:
            try:
                info = await c.stat(self._path(path))
            except (aioftp.StatusCodeError, FileNotFoundError) as e:
                raise SourceInvalidError(f"не найдено: {self.uri(path)}") from e
        return self._mk(self._path(path), dict(info))

    async def list(self, path: str = "", recursive: bool = False) -> AsyncIterator[Entry]:
        async with self._conn() as c:
            try:
                async for p, info in c.list(self._path(path), recursive=recursive):
                    if info.get("type") in ("dir", "cdir", "pdir"):
                        continue
                    if str(p).rstrip("/").endswith((".", "..")):
                        continue
                    yield self._mk(p, dict(info))
            except aioftp.StatusCodeError as e:
                raise StorageError(f"FTP list {path}: {e}") from e

    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
            return True
        except SourceInvalidError:
            return False

    async def _reader(self, path: str, offset: int) -> AsyncIterator[bytes]:
        async with self._conn() as c:
            try:
                cm = c.get_stream(self._path(path), offset=offset)
            except TypeError:
                # старый aioftp без REST-смещения
                if offset:
                    raise StorageError("FTP: клиент не поддерживает resume (REST)") from None
                cm = c.get_stream(self._path(path))
            async with cm as stream:
                while data := await stream.read(self.chunk):
                    yield data

    async def _writer(self, path: str, chunks: AsyncIterator[bytes], size: int | None) -> None:
        async with self._conn() as c:
            async with c.put_stream(self._path(path)) as stream:
                async for data in chunks:
                    await stream.write(data)

    async def _publish(self, tmp: str, final: str, overwrite: bool) -> None:
        async with self._conn() as c:
            if overwrite:
                try:
                    await c.remove_file(self._path(final))
                except aioftp.StatusCodeError:
                    pass
            try:
                await c.rename(self._path(tmp), self._path(final))
            except aioftp.StatusCodeError as e:
                raise StorageError(f"FTP rename {tmp}->{final}: {e}") from e

    async def delete(self, path: str) -> None:
        async with self._conn() as c:
            try:
                await c.remove_file(self._path(path))
            except aioftp.StatusCodeError:
                pass

    async def mkdirs(self, path: str) -> None:
        async with self._conn() as c:
            await c.make_directory(self._path(path), parents=True)

    async def move(self, src: str, dst: str) -> None:
        self._guard_write()
        async with self._conn() as c:
            await c.rename(self._path(src), self._path(dst))