"""Internal API для воркеров (Workers → MU на схеме). Секреты не отдаются никогда."""
from __future__ import annotations

import hmac
import uuid

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from mf_core import queue as q
from mf_core.models import Artifact, Job, Storage
from ui.deps import db_session, settings_dep

router = APIRouter(prefix="/internal/v1", tags=["internal"])


async def guard(authorization: str = Header(""), s=Depends(settings_dep)):
    if not s.ui_internal_token:
        raise HTTPException(503, "internal API выключен (MF_UI_INTERNAL_TOKEN не задан)")
    expected = f"Bearer {s.ui_internal_token}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "неверный internal-токен")
    return True


@router.get("/jobs/{jid}", dependencies=[Depends(guard)])
async def get_job(jid: uuid.UUID, sess: AsyncSession = Depends(db_session)):
    job = await sess.get(Job, jid)
    if job is None:
        raise HTTPException(404)
    return {"id": job.id, "spec": job.spec, "params": job.params,
            "cancel_requested": job.cancel_requested, "attempts": job.attempts}


@router.post("/jobs/{jid}/events", dependencies=[Depends(guard)])
async def add_event(jid: uuid.UUID, payload: dict = Body(...),
                    sess: AsyncSession = Depends(db_session)):
    await q.add_event(sess, jid, payload.get("type", "info"), payload.get("message", ""),
                      payload.get("level", "info"), payload.get("data"))
    await sess.commit()
    return {"ok": True}


@router.post("/jobs/{jid}/artifacts", status_code=201, dependencies=[Depends(guard)])
async def add_artifact(jid: uuid.UUID, payload: dict = Body(...),
                       sess: AsyncSession = Depends(db_session)):
    storage = await sess.get(Storage, uuid.UUID(payload["storage_id"]))
    if storage is None:
        raise HTTPException(400, "неизвестное хранилище")
    obj = Artifact(job_id=jid, storage_id=storage.id, uri=payload["uri"],
                   role=payload.get("role", "output"), size=payload.get("size"),
                   checksum=payload.get("checksum"), probe=payload.get("probe") or {})
    sess.add(obj)
    await sess.commit()
    return {"id": obj.id}


@router.get("/storages", dependencies=[Depends(guard)])
async def storages(sess: AsyncSession = Depends(db_session)):
    from sqlalchemy import select
    rows = (await sess.execute(select(Storage).where(Storage.enabled))).scalars().all()
    return [{"id": r.id, "name": r.name, "kind": r.kind, "base_uri": r.base_uri,
             "mount_path": r.mount_path, "options": r.options, "read_only": r.read_only,
             "secret_ref": r.secret_ref} for r in rows]