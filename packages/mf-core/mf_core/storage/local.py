"""local и nfs (POSIX). NFS через RWX PVC либо прямой mount — код одинаковый."""
from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import aiofiles

from mf_core.errors import SourceInvalidError, StorageAuthError, StorageError
from mf_core.storage.base import Backend, Entry


class LocalBackend(Backend):
    kind = "local"

    def __init__(self, row, creds=None):
        super().__init__(row, creds)
        base = row.base_uri
        if base.startswith("file://"):
            base = base[7:]
        self.root = Path(row.mount_path or base).expanduser()

    def _abs(self, path: str) -> Path:
        root = self.root.resolve()
        target = (root / path.lstrip("/")).resolve()
        if target != root and not str(target).startswith(str(root) + os.sep):
            raise StorageAuthError(f"выход за пределы хранилища: {path!r}")
        from mf_core.config import get_settings
        allow = get_settings().storage_local_allow_paths
        if allow and not any(
                str(target).startswith(str(Path(a).resolve())) for a in allow
        ):
            raise StorageAuthError(f"путь не в allowlist: {target}")
        return target

    def _entry(self, p: Path, st: os.stat_result) -> Entry:
        rel = str(p.relative_to(self.root.resolve()))
        return Entry(
            path=rel, uri=self.uri(rel), size=st.st_size, mtime=st.st_mtime,
            is_dir=os.path.isdir(p), extra={"inode": st.st_ino, "dev": st.st_dev},
        )

    async def stat(self, path: str) -> Entry:
        p = self._abs(path)
        try:
            st = await asyncio.to_thread(p.stat)
        except FileNotFoundError as e:
            raise SourceInvalidError(f"не найдено: {self.uri(path)}") from e
        return self._entry(p, st)

    async def list(self, path: str = "", recursive: bool = False) -> AsyncIterator[Entry]:
        root = self._abs(path)

        def scan() -> list[Entry]:
            out: list[Entry] = []
            stack = [root]
            while stack:
                cur = stack.pop()
                try:
                    with os.scandir(cur) as it:
                        for de in it:
                            p = Path(de.path)
                            try:
                                st = de.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            if de.is_dir(follow_symlinks=False):
                                if recursive:
                                    stack.append(p)
                                continue
                            out.append(self._entry(p, st))
                except (PermissionError, FileNotFoundError):
                    continue
            return out

        for e in await asyncio.to_thread(scan):
            yield e

    async def exists(self, path: str) -> bool:
        return await asyncio.to_thread(self._abs(path).exists)

    async def _reader(self, path: str, offset: int) -> AsyncIterator[bytes]:
        p = self._abs(path)
        async with aiofiles.open(p, "rb") as f:
            if offset:
                await f.seek(offset)
            while data := await f.read(self.chunk):
                yield data

    async def _writer(self, path: str, chunks: AsyncIterator[bytes], size: int | None) -> None:
        p = self._abs(path)
        await asyncio.to_thread(p.parent.mkdir, parents=True, exist_ok=True)
        async with aiofiles.open(p, "wb") as f:
            async for c in chunks:
                await f.write(c)
            await f.flush()
            os.fsync(f.fileno())

    async def _publish(self, tmp: str, final: str, overwrite: bool) -> None:
        src, dst = self._abs(tmp), self._abs(final)
        if dst.exists() and not overwrite:
            raise StorageError(f"цель появилась во время записи: {dst}")
        await asyncio.to_thread(os.replace, src, dst)

    async def delete(self, path: str) -> None:
        p = self._abs(path)
        def _rm():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        await asyncio.to_thread(_rm)

    async def mkdirs(self, path: str) -> None:
        await asyncio.to_thread(self._abs(path).mkdir, parents=True, exist_ok=True)

    async def move(self, src: str, dst: str) -> None:
        self._guard_write()
        s, d = self._abs(src), self._abs(dst)
        await asyncio.to_thread(d.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(s), str(d))

    async def free_space(self) -> int | None:
        try:
            return (await asyncio.to_thread(shutil.disk_usage, str(self.root))).free
        except OSError:
            return None