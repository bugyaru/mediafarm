"""Парсеры манифестов/метаданных → список источников для конвертации.

Кастомные парсеры подключаются entry-point'ом группы ``mediafarm.parsers``
(объект с атрибутом ``id`` и async-методом ``parse(ctx)``).
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from mf_core.errors import ConfigInvalidError
from mf_core.storage.base import Backend, Entry

MAX_MANIFEST_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SourceSpec:
    uri: str
    kind: str = "file"                       # file | url | stream
    meta: dict[str, Any] = field(default_factory=dict)
    preset_name: str | None = None
    priority: int | None = None
    output: dict[str, Any] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    identity: str | None = None              # явный ключ дедупликации (id из XML и т.п.)


@dataclass(slots=True)
class ParseContext:
    backend: Backend
    entry: Entry
    options: dict[str, Any] = field(default_factory=dict)
    base_uri: str = ""

    async def read_bytes(self, limit: int = MAX_MANIFEST_BYTES) -> bytes:
        return await self.backend.read_range(self.entry.path, 0, limit)

    async def read_text(self, encoding: str | None = None) -> str:
        enc = encoding or self.options.get("encoding", "utf-8")
        return (await self.read_bytes()).decode(enc, "replace")

    def resolve(self, ref: str) -> str:
        """Относительная ссылка → абсолютный URI в терминах хранилища."""
        ref = (ref or "").strip()
        if not ref:
            return ""
        if "://" in ref:
            return ref
        base = self.entry.uri
        if base.startswith(("http://", "https://")):
            return urljoin(base, ref)
        u = urlsplit(base)
        if ref.startswith("/"):
            return urlunsplit((u.scheme, u.netloc, ref, "", "")) if u.scheme else ref
        path = posixpath.normpath(posixpath.join(posixpath.dirname(u.path), ref))
        return urlunsplit((u.scheme, u.netloc, path, "", ""))


class Parser(Protocol):
    id: str
    async def parse(self, ctx: ParseContext) -> list[SourceSpec]: ...


REGISTRY: dict[str, Parser] = {}


def register(name: str):
    def deco(cls):
        instance = cls()
        instance.id = name
        REGISTRY[name] = instance
        return cls
    return deco


def get(name: str) -> Parser:
    load_plugins()
    if name not in REGISTRY:
        raise ConfigInvalidError(f"парсер {name!r} не найден; доступны: {sorted(REGISTRY)}")
    return REGISTRY[name]


def available() -> list[str]:
    load_plugins()
    return sorted(REGISTRY)


_plugins_loaded = False


def load_plugins() -> None:
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    from importlib.metadata import entry_points
    for ep in entry_points(group="mediafarm.parsers"):
        try:
            obj = ep.load()
            instance = obj() if isinstance(obj, type) else obj
            REGISTRY[getattr(instance, "id", ep.name)] = instance
        except Exception:  # noqa: BLE001
            continue


def spec_from_row(
        ctx: ParseContext, row: dict[str, Any], *, url_field: str = "url",
        url_template: str | None = None, preset_field: str = "preset",
        priority_field: str = "priority", identity_field: str = "id",
        meta_fields: list[str] | None = None, default_kind: str | None = None,
) -> SourceSpec | None:
    """Общий конструктор SourceSpec для табличных парсеров (xml/csv/json)."""
    if url_template:
        try:
            raw = url_template.format(**row)
        except (KeyError, IndexError):
            return None
    else:
        raw = row.get(url_field)
    if raw is None or not str(raw).strip():
        return None
    uri = ctx.resolve(str(raw).strip())
    kind = default_kind or ("url" if uri.startswith(("http://", "https://")) else "file")
    meta = {
        k: v for k, v in row.items()
        if v not in (None, "") and (meta_fields is None or k in meta_fields)
    }
    preset = row.get(preset_field)
    try:
        prio = int(row[priority_field]) if row.get(priority_field) not in (None, "") else None
    except (TypeError, ValueError):
        prio = None
    ident = row.get(identity_field)
    return SourceSpec(
        uri=uri, kind=kind, meta=meta,
        preset_name=str(preset) if preset else None, priority=prio,
        identity=str(ident) if ident else None,
    )


from mf_core.parsers import csv as _csv, json as _json, m3u8 as _m3u8, mpd as _mpd, xml as _xml  # noqa: E402,F401