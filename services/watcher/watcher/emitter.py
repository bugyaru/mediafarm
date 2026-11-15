"""Создание asset + job в одной транзакции. Идемпотентность на обоих уровнях."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mf_core import queue as q
from mf_core.config import Settings
from mf_core.errors import ConfigInvalidError
from mf_core.models import Preset
from mf_core.parsers import SourceSpec
from mf_core.schemas import PresetSpec
from mf_core.storage import StorageRegistry
from mf_core.storage.template import build_context, render
from mf_core.telemetry import get_logger
from mf_core.util import asset_identity, job_idempotency_key

log = get_logger("mf.emitter")

INSERT_ASSET = text("""
INSERT INTO asset (identity_key, watch_folder_id, storage_id, uri, kind, size, mtime,
                   fingerprint, meta, parent_id, state)
VALUES (:ik, :wf, :st, :uri, :kind, :size, :mtime, :fp, CAST(:meta AS jsonb), :parent, :state)
ON CONFLICT (identity_key) DO NOTHING
RETURNING id
""")


@dataclass(slots=True)
class EmitResult:
    asset_id: uuid.UUID | None
    job_id: uuid.UUID | None
    created: bool
    reason: str = ""


class Emitter:
    def __init__(self, registry: StorageRegistry, settings: Settings) -> None:
        self.registry, self.s = registry, settings

    async def register_manifest(
            self, sess: AsyncSession, wf: dict, *, uri: str, storage_id: uuid.UUID | None,
            fingerprint: str, size: int | None, mtime: float | None, meta: dict | None = None,
    ) -> uuid.UUID | None:
        ik = asset_identity(storage_id, uri, fingerprint)
        row = (await sess.execute(INSERT_ASSET, {
            "ik": ik, "wf": wf["id"], "st": storage_id, "uri": uri, "kind": "file",
            "size": size, "mtime": _ts(mtime), "fp": fingerprint,
            "meta": json.dumps({"role": "manifest", **(meta or {})}, ensure_ascii=False),
            "parent": None, "state": "skipped",
        })).first()
        if row:
            return row[0]
        return (await sess.execute(text("SELECT id FROM asset WHERE identity_key=:k"),
                                   {"k": ik})).scalar_one_or_none()

    async def emit(
            self, sess: AsyncSession, wf: dict, src: SourceSpec, preset: Preset, spec: PresetSpec,
            *, storage_id: uuid.UUID | None = None, fingerprint: str | None = None,
            size: int | None = None, mtime: float | None = None,
            parent_asset_id: uuid.UUID | None = None,
    ) -> EmitResult:
        fp = fingerprint or src.identity or "url"
        ik = asset_identity(storage_id, src.uri, fp)
        row = (await sess.execute(INSERT_ASSET, {
            "ik": ik, "wf": wf["id"], "st": storage_id, "uri": src.uri, "kind": src.kind,
            "size": size, "mtime": _ts(mtime), "fp": fp,
            "meta": json.dumps(src.meta, ensure_ascii=False, default=str),
            "parent": parent_asset_id, "state": "new",
        })).first()

        if row:
            asset_id, fresh = row[0], True
        else:
            existing = (await sess.execute(
                text("SELECT id, state FROM asset WHERE identity_key=:k"), {"k": ik}
            )).first()
            if existing is None:
                return EmitResult(None, None, False, "race")
            asset_id, state = existing[0], existing[1]
            if state != "new":
                return EmitResult(asset_id, None, False, f"asset_state:{state}")
            fresh = False

        tpl = wf.get("job_template") or {}
        output_uri = self._output_uri(src, spec, preset)
        idem = job_idempotency_key(ik, preset.name, preset.version, output_uri)

        params: dict[str, Any] = {"asset_meta": src.meta}
        if src.kind == "file":
            params["input_uri"] = src.uri
        else:
            params["url"] = src.uri
        if src.output:
            params["output"] = src.output
        if tpl.get("log_storage"):
            params["log_storage"] = tpl["log_storage"]
        if tpl.get("overrides"):
            params["overrides"] = tpl["overrides"]
        params.update(src.params or {})
        params.update(tpl.get("params") or {})

        job_id, created = await q.enqueue(
            sess,
            kind=src.params.get("kind") or tpl.get("kind") or "transcode",
            preset_id=preset.id, preset_version=preset.version,
            spec=spec.to_db(), params=params,
            asset_id=asset_id, watch_folder_id=wf["id"],
            priority=src.priority if src.priority is not None else int(tpl.get("priority", 100)),
            selector=spec.selector, requires_gpu=spec.requires_gpu,
            gpu_sessions=spec.gpu_sessions, cpu_cost=spec.cpu_cost,
            concurrency_key=wf.get("concurrency_key"),
            concurrency_limit=int(wf.get("max_concurrency") or 0),
            max_attempts=spec.max_attempts, idempotency_key=idem,
            created_by=f"watcher:{wf['name']}",
        )
        if created:
            await sess.execute(
                text("UPDATE asset SET state='planned', updated_at=now() WHERE id=:i"),
                {"i": asset_id},
            )
        return EmitResult(asset_id, job_id, created, "" if created else "duplicate_job")

    def _output_uri(self, src: SourceSpec, spec: PresetSpec, preset: Preset) -> str:
        publish = next((s for s in spec.steps if s.id == "publish"), None)
        override = src.output or {}
        storage = override.get("storage") or (publish.storage if publish else "")
        template = override.get("path") or (publish.path if publish else "")
        if not template:
            return f"{storage}/<job>"
        if "{job." in template:            # job.id ещё неизвестен — ключ по шаблону
            return f"{storage}/{template}"
        try:
            ctx = build_context(asset_uri=src.uri, asset_meta=src.meta, preset_name=preset.name)
            rendered = render(template, ctx)
        except ConfigInvalidError:
            rendered = template
        try:
            base = self.registry.row_by_name(storage).base_uri.rstrip("/") if storage else ""
        except ConfigInvalidError:
            base = storage or ""
        return f"{base}/{rendered}" if base else rendered


def _ts(mtime: float | None) -> dt.datetime | None:
    if not mtime:
        return None
    try:
        return dt.datetime.fromtimestamp(mtime, dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None