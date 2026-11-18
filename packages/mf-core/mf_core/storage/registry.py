from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from sqlalchemy import select

from mf_core.errors import ConfigInvalidError
from mf_core.models import Storage
from mf_core.secrets import SecretResolver
from mf_core.storage.base import Backend, StorageRow
from mf_core.storage.local import LocalBackend
from mf_core.storage.smb import SmbBackend
from mf_core.storage.s3 import S3Backend
from mf_core.storage.ftp import FtpBackend

BUILDERS: dict[str, type] = {
    "local": LocalBackend,
    "smb": SmbBackend,
    "s3": S3Backend,
    "ftp": FtpBackend,
}

try:
    from mf_core.storage import nfs as _nfs
    if _nfs.NFS is not None:
        BUILDERS["nfs"] = _nfs.NfsBackend
    else:
        BUILDERS["nfs"] = LocalBackend
except ImportError:
    BUILDERS["nfs"] = LocalBackend

@dataclass(frozen=True, slots=True)
class ParsedUri:
    scheme: str
    host: str
    port: int | None
    path: str


def parse_uri(uri: str) -> ParsedUri:
    if "://" not in uri:
        return ParsedUri("file", "", None, uri)
    u = urlsplit(uri)
    return ParsedUri(u.scheme.lower(), (u.hostname or "").lower(), u.port, unquote(u.path))


def split_uri(uri: str) -> tuple[str, str]:
    p = parse_uri(uri)
    base = f"{p.scheme}://{p.host}" + (f":{p.port}" if p.port else "") if p.host else f"{p.scheme}://"
    return base, p.path.lstrip("/")


def _norm(uri: str) -> str:
    p = parse_uri(uri)
    path = p.path.rstrip("/")
    if p.scheme == "file":
        return f"file://{path}"
    netloc = p.host + (f":{p.port}" if p.port else "")
    return f"{p.scheme}://{netloc}{path}"


class StorageRegistry:
    """Кэш описаний хранилищ и построенных backend'ов. Refresh по TTL из БД."""

    def __init__(self, ttl_s: float = 30.0) -> None:
        self._rows: dict[str, StorageRow] = {}
        self._by_name: dict[str, StorageRow] = {}
        self._backends: dict[tuple, Backend] = {}
        self._secrets = SecretResolver()
        self._ttl = ttl_s
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self, session) -> None:
        rows = (await session.execute(select(Storage).where(Storage.enabled.is_(True)))).scalars().all()
        async with self._lock:
            self._rows = {_norm(r.base_uri): StorageRow.from_orm(r) for r in rows}
            self._by_name = {r.name: StorageRow.from_orm(r) for r in rows}
            self._loaded_at = asyncio.get_running_loop().time()
            alive = {row.id for row in self._rows.values()}
            for key in list(self._backends):
                if key[0] not in alive:
                    await self._backends.pop(key).close()

    async def ensure_fresh(self, session) -> None:
        if asyncio.get_running_loop().time() - self._loaded_at > self._ttl:
            await self.refresh(session)

    def _match(self, uri: str) -> StorageRow:
        target = _norm(uri)
        best: tuple[int, StorageRow] | None = None
        for base, row in self._rows.items():
            if target == base or target.startswith(base + "/"):
                if best is None or len(base) > best[0]:
                    best = (len(base), row)
        if best is None:
            raise ConfigInvalidError(f"нет хранилища для URI: {uri}")
        return best[1]

    def _build(self, row: StorageRow) -> Backend:
        fp = self._secrets.fingerprint(row.secret_ref)
        key = (row.id, fp, row.mount_path or "", repr(sorted(row.options.items())))
        if key not in self._backends:
            creds = self._secrets.resolve(row.secret_ref)
            self._backends[key] = BUILDERS[row.kind](row, creds)
        return self._backends[key]

    def resolve(self, uri: str) -> tuple[Backend, str]:
        row = self._match(uri)
        base = _norm(row.base_uri)
        rel = _norm(uri)[len(base):].lstrip("/")
        return self._build(row), rel

    def by_name(self, name: str) -> Backend:
        row = self._by_name.get(name)
        if row is None:
            raise ConfigInvalidError(f"хранилище {name!r} не найдено или выключено")
        return self._build(row)

    def row_by_name(self, name: str) -> StorageRow:
        if name not in self._by_name:
            raise ConfigInvalidError(f"хранилище {name!r} не найдено")
        return self._by_name[name]

    async def close(self) -> None:
        async with self._lock:
            for b in self._backends.values():
                await b.close()
            self._backends.clear()