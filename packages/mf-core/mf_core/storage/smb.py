"""SMB2/3 через smbprotocol (userspace, без privileged-монтирования в поде)."""
from __future__ import annotations

import asyncio
import ntpath
import threading
from collections.abc import AsyncIterator
from urllib.parse import unquote, urlsplit

import smbclient

from mf_core.config import get_settings
from mf_core.errors import SourceInvalidError, StorageError
from mf_core.storage.base import Backend, Entry

_SESSIONS: set[tuple] = set()
_LOCK = threading.Lock()


def _unc(host: str, share_path: str) -> str:
    return "\\\\" + host + "\\" + share_path.replace("/", "\\").strip("\\")


class SmbBackend(Backend):
    kind = "smb"

    def __init__(self, row, creds=None):
        super().__init__(row, creds)
        u = urlsplit(row.base_uri)
        self.host = u.hostname or ""
        self.port = int(u.port or self.options.get("port", 445))
        parts = unquote(u.path).strip("/").split("/", 1)
        self.share = parts[0]
        self.prefix = parts[1] if len(parts) > 1 else ""
        if not self.host or not self.share:
            raise StorageError(f"некорректный SMB URI: {row.base_uri}")

    def _ensure_session(self, *, force: bool = False) -> None:
        key = (self.host, self.port, self.creds.get("username", ""))
        with _LOCK:
            if not force and key in _SESSIONS:
                return
            kwargs: dict = {
                "port": self.port,
                "connection_timeout": get_settings().storage_smb_timeout_s,
                "username": self.creds.get("username") or None,
                "password": self.creds.get("password") or None,
            }
            if self.creds.get("domain"):
                kwargs["username"] = f"{self.creds['domain']}{kwargs['username']}"
            if self.options.get("kerberos"):
                kwargs["auth_protocol"] = "kerberos"
            if self.options.get("encrypt", False):
                kwargs["require_encryption"] = True
            try:
                smbclient.register_session(self.host, **kwargs)
            finally:
                _SESSIONS.add(key)

    async def _writer(self, path: str, chunks: AsyncIterator[bytes], size: int | None) -> None:
        def _open():
            self._ensure_session()
            return smbclient.open_file(self._path(path), mode="wb",
                                       buffering=0, share_access="rwd")   # rwd: не блокировать rename
        f = await asyncio.to_thread(_open)
        try:
            async for c in chunks:
                await asyncio.to_thread(f.write, c)
            await asyncio.to_thread(f.flush)                               # явный сброс на шару
        finally:
            await asyncio.to_thread(f.close)

    async def _publish(self, tmp: str, final: str, overwrite: bool) -> None:
        def _do():
            src, dst = self._path(tmp), self._path(final)
            if smbclient.path.exists(dst):
                if not overwrite:
                    raise StorageError(f"цель существует: {final}")
                smbclient.remove(dst)
            if not smbclient.path.exists(src):
                # stale-сессия: перерегистрируем и пробуем ещё раз
                self._ensure_session(force=True)
                if not smbclient.path.exists(src):
                    raise StorageError(
                        f"временный объект не найден на шаре: {src} "
                        f"(запись не завершена или сессия/кодировка UNC)")
            try:
                smbclient.rename(src, dst)
            except OSError:
                # fallback: copy+remove (rename капризен к кириллице/пробелам в smbclient)
                with smbclient.open_file(src, mode="rb", buffering=0, share_access="r") as r, \
                        smbclient.open_file(dst, mode="wb", buffering=0, share_access="w") as w:
                    while data := r.read(self.chunk):
                        w.write(data)
                smbclient.remove(src)
        await asyncio.to_thread(_do)

    def _path(self, path: str) -> str:
        rel = "/".join(p for p in (self.prefix, path.strip("/")) if p)
        return _unc(self.host, f"{self.share}/{rel}")

    def _rel(self, unc: str) -> str:
        parts = unc.replace("\\", "/").split("/")
        # //host/share/prefix/... -> отсекаем host, share, prefix
        skip = 2 + len([p for p in self.prefix.split("/") if p])
        return "/".join(parts[skip:])

    def _mk(self, unc: str, st) -> Entry:
        rel = self._rel(unc)
        import stat as statmod
        return Entry(
            path=rel, uri=self.uri(rel), size=int(st.st_size), mtime=float(st.st_mtime),
            is_dir=statmod.S_ISDIR(st.st_mode), extra={"inode": getattr(st, "st_ino", None)},
        )

    async def stat(self, path: str) -> Entry:
        def _do():
            self._ensure_session()
            return smbclient.stat(self._path(path))
        try:
            st = await asyncio.to_thread(_do)
        except FileNotFoundError as e:
            raise SourceInvalidError(f"не найдено: {self.uri(path)}") from e
        except OSError as e:
            raise StorageError(f"SMB stat {path}: {e}") from e
        return self._mk(self._path(path), st)

    async def list(self, path: str = "", recursive: bool = False) -> AsyncIterator[Entry]:
        def _scan() -> list[Entry]:
            self._ensure_session()
            out: list[Entry] = []
            stack = [self._path(path)]
            while stack:
                cur = stack.pop()
                try:
                    with smbclient.scandir(cur) as it:
                        for de in it:
                            if de.name in (".", ".."):
                                continue
                            try:
                                st = de.stat()
                            except OSError:
                                continue
                            if de.is_dir():
                                if recursive:
                                    stack.append(de.path)
                                continue
                            out.append(self._mk(de.path, st))
                except OSError:
                    continue
            return out
        for e in await asyncio.to_thread(_scan):
            yield e

    async def exists(self, path: str) -> bool:
        def _do():
            self._ensure_session()
            return smbclient.path.exists(self._path(path))
        return bool(await asyncio.to_thread(_do))

    async def _reader(self, path: str, offset: int) -> AsyncIterator[bytes]:
        def _open():
            self._ensure_session()
            f = smbclient.open_file(self._path(path), mode="rb", buffering=0, share_access="r")
            if offset:
                f.seek(offset)
            return f
        f = await asyncio.to_thread(_open)
        try:
            while data := await asyncio.to_thread(f.read, self.chunk):
                yield data
        finally:
            await asyncio.to_thread(f.close)


    async def delete(self, path: str) -> None:
        def _do():
            self._ensure_session()
            p = self._path(path)
            if smbclient.path.isdir(p):
                smbclient.removedirs(p)
            else:
                smbclient.remove(p)
        await asyncio.to_thread(_do)

    async def mkdirs(self, path: str) -> None:
        def _do():
            self._ensure_session()
            smbclient.makedirs(self._path(path), exist_ok=True)
        await asyncio.to_thread(_do)

    async def move(self, src: str, dst: str) -> None:
        self._guard_write()
        def _do():
            self._ensure_session()
            d = self._path(dst)
            smbclient.makedirs(ntpath.dirname(d), exist_ok=True)
            smbclient.rename(self._path(src), d)
        await asyncio.to_thread(_do)