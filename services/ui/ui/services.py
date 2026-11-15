from __future__ import annotations

import hashlib
import json
import time
import uuid
from fnmatch import fnmatch
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mf_core import queue as q
from mf_core.config import Settings
from mf_core.errors import ConfigInvalidError
from mf_core.media.presets import apply_overrides, load_preset, validate_spec
from mf_core.models import Asset, AuditLog, Job, Preset, Storage, WatchFolder, Worker
from mf_core.parsers import ParseContext, available as parsers_available, get as get_parser
from mf_core.schemas import JobCreate, PresetSpec
from mf_core.secrets import SecretResolver
from mf_core.storage import StorageRegistry
from mf_core.storage.base import StorageRow
from mf_core.storage.registry import BUILDERS
from mf_core.storage.template import build_context, render
from mf_core.telemetry import get_logger

log = get_logger("mf.ui.services")


async def audit(sess: AsyncSession, actor: str | None, action: str, target_type: str = "",
                target_id: str = "", data: dict | None = None, ip: str | None = None) -> None:
    sess.add(AuditLog(actor=actor, action=action, target_type=target_type,
                      target_id=str(target_id), ip=ip,
                      data=json.loads(json.dumps(data or {}, default=str))))


# ------------------------------- задачи ------------------------------------
def output_uri(spec: PresetSpec, preset: Preset, src_uri: str, meta: dict,
               registry: StorageRegistry, override: dict | None = None) -> str:
    override = override or {}
    publish = next((s for s in spec.steps if s.id == "publish"), None)
    storage = override.get("storage") or (publish.storage if publish else "")
    template = override.get("path") or (publish.path if publish else "")
    if not template:
        return f"{storage}/<job>"
    if "{job." in template:
        return f"{storage}/{template}"
    try:
        rendered = render(template, build_context(asset_uri=src_uri, asset_meta=meta,
                                                  preset_name=preset.name))
    except ConfigInvalidError:
        rendered = template
    try:
        base = registry.row_by_name(storage).base_uri.rstrip("/") if storage else ""
    except ConfigInvalidError:
        base = storage or ""
    return f"{base}/{rendered}"


async def create_job(sess: AsyncSession, data: JobCreate, actor: str,
                     registry: StorageRegistry) -> tuple[uuid.UUID, bool, str]:
    preset = await load_preset(sess, preset_id=data.preset_id, name=data.preset_name)
    spec = validate_spec(apply_overrides(dict(preset.spec), data.params.get("overrides")))

    asset: Asset | None = None
    if data.asset_id:
        asset = await sess.get(Asset, data.asset_id)
        if asset is None:
            raise ConfigInvalidError(f"ассет {data.asset_id} не найден")
    src_uri = data.url or data.input_uri or (asset.uri if asset else "")
    if not src_uri:
        raise ConfigInvalidError("не задан источник")
    meta = (asset.meta if asset else {}) or data.params.get("asset_meta", {})

    params: dict[str, Any] = dict(data.params)
    if data.url:
        params["url"] = data.url
    else:
        params["input_uri"] = src_uri
    params.setdefault("asset_meta", meta)

    out = output_uri(spec, preset, src_uri, meta, registry, params.get("output"))
    idem_src = asset.identity_key if asset else hashlib.sha256(src_uri.encode()).hexdigest()
    idem = hashlib.sha256(f"{idem_src}|{preset.name}|{preset.version}|{out}".encode()).hexdigest()

    job_id, created = await q.enqueue(
        sess, kind=data.kind, preset_id=preset.id, preset_version=preset.version,
        spec=spec.to_db(), params=params, asset_id=asset.id if asset else None,
        priority=data.priority, selector=spec.selector, requires_gpu=spec.requires_gpu,
        gpu_sessions=spec.gpu_sessions, cpu_cost=spec.cpu_cost,
        max_attempts=data.max_attempts, run_after=data.run_after,
        idempotency_key=idem, created_by=actor,
    )
    if created and asset:
        await sess.execute(text("UPDATE asset SET state='planned' WHERE id=:i"), {"i": asset.id})
    return job_id, created, out


# ------------------------------ хранилища ----------------------------------
async def test_storage(row: Storage, writable_probe: bool = True) -> dict[str, Any]:
    creds = SecretResolver().resolve(row.secret_ref)
    be = BUILDERS[row.kind](StorageRow.from_orm(row), creds)
    started = time.monotonic()
    result: dict[str, Any] = {"kind": row.kind, "entries": [], "writable": None}
    try:
        items = []
        async for e in be.list("", recursive=False):
            items.append({"path": e.path, "size": e.size, "is_dir": e.is_dir})
            if len(items) >= 5:
                break
        result["entries"] = items
        if writable_probe and not row.read_only:
            probe = f".mf-probe-{uuid.uuid4().hex[:8]}"
            await be.write_bytes(probe, b"mediafarm", overwrite=True)
            await be.delete(probe)
            result["writable"] = True
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        result["ok"] = True
    except Exception as e:  # noqa: BLE001
        result.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        await be.close()
    return result


# -------------------------------- пресеты ----------------------------------
async def validate_preset(sess: AsyncSession, raw: dict) -> dict[str, Any]:
    spec = validate_spec(raw)
    rows = (await sess.execute(select(Worker).where(Worker.state != "offline"))).scalars().all()
    encoders: set[str] = set()
    cuda_filters: set[str] = set()
    has_cuda = False
    for w in rows:
        caps = w.capabilities or {}
        encoders |= set(caps.get("encoders") or [])
        cuda_filters |= set(caps.get("cuda_filters") or [])
        has_cuda = has_cuda or bool(caps.get("cuda"))
    warnings: list[str] = []
    for step in spec.steps:
        if step.engine == "ffmbc":
            warnings.append("ffmbc: только CPU")
        if step.id == "transcode" and step.video and not step.video.copy:
            if step.video.codec.endswith("_nvenc") and encoders and step.video.codec not in encoders:
                warnings.append(f"{step.video.codec} нет ни на одном воркере → fallback CPU")
        if step.scale and has_cuda and not cuda_filters:
            warnings.append("нет CUDA-фильтров: масштабирование на CPU")
    if spec.requires_gpu and rows and not has_cuda:
        warnings.append("пресет требует GPU, но GPU-воркеров нет — задача будет ждать")
    return {"ok": True, "spec": spec.to_db(), "warnings": warnings,
            "workers": len(rows), "gpu_workers": sum(1 for w in rows if (w.capabilities or {}).get("cuda"))}


# ------------------------------ dry-run папки -------------------------------
async def dry_run_folder(sess: AsyncSession, wf: WatchFolder, registry: StorageRegistry,
                         limit: int = 20) -> dict[str, Any]:
    storage = await sess.get(Storage, wf.storage_id)
    base_uri = storage.base_uri.rstrip("/")
    be, rel = registry.resolve(f"{base_uri}/{wf.path.lstrip('/')}")
    include, exclude = wf.include or ["*"], wf.exclude or []
    items, sources, errors = [], [], []
    async for e in be.list(rel, wf.recursive):
        if e.is_dir:
            continue
        if any(fnmatch(e.name, p) or fnmatch(e.path, p) for p in exclude + ["*.part", ".*"]):
            continue
        if not any(fnmatch(e.name, p) or fnmatch(e.path, p) for p in include):
            continue
        items.append({"path": e.path, "size": e.size, "fingerprint": e.fingerprint})
        if wf.parser_id:
            try:
                ctx = ParseContext(backend=be, entry=e, options=wf.parser_opts or {})
                for src in await get_parser(wf.parser_id).parse(ctx):
                    sources.append({"uri": src.uri, "kind": src.kind, "meta": src.meta,
                                    "preset": src.preset_name})
            except Exception as ex:  # noqa: BLE001
                errors.append({"path": e.path, "error": str(ex)})
        else:
            sources.append({"uri": e.uri, "kind": "file", "meta": {}})
        if len(items) >= limit:
            break
    return {"matched": len(items), "items": items, "sources": sources[:limit],
            "errors": errors, "parsers": parsers_available(), "truncated": len(items) >= limit}