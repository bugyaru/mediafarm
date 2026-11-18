from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mf_core import queue as q
from mf_core.errors import ConfigInvalidError, MFError
from mf_core.media.presets import BUILTIN_PRESETS, seed_presets, validate_spec
from mf_core.models import (
    Artifact, Asset, AuditLog, Job, JobEvent, JobStep, Preset, Schedule, Storage, WatchFolder, Worker,
)
from mf_core.parsers import available as parsers_available
from mf_core.schemas import JobCreate, ScheduleIn, StorageIn, WatchFolderIn
from ui.deps import db_session, require_role
from ui.services import audit, create_job, dry_run_folder, test_storage, validate_preset

router = APIRouter(prefix="/api/v1", tags=["api"])
op = require_role("operator")
admin = require_role("admin")
viewer = require_role("viewer")


def _dump(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


async def _get(sess: AsyncSession, model, obj_id: uuid.UUID):
    obj = await sess.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} не найден")
    return obj


# --------------------------------- stats -----------------------------------
@router.get("/stats")
async def stats(sess: AsyncSession = Depends(db_session), user=Depends(viewer)):
    rows = await q.stats(sess)
    workers = (await sess.execute(select(Worker))).scalars().all()
    return {
        "queue": rows,
        "workers": [{"id": w.id, "kind": w.kind, "state": w.state,
                     "cpu": [w.cpu_used, w.cpu_slots], "gpu": [w.gpu_used, w.gpu_slots],
                     "heartbeat_at": w.heartbeat_at} for w in workers],
        "totals": {
            "assets": await sess.scalar(select(func.count()).select_from(Asset)),
            "presets": await sess.scalar(select(func.count()).select_from(Preset)),
            "folders": await sess.scalar(select(func.count()).select_from(WatchFolder)),
        },
    }


@router.get("/parsers")
async def parsers(user=Depends(viewer)):
    return {"parsers": parsers_available(), "builtin_presets": sorted(BUILTIN_PRESETS)}


# -------------------------------- storages ---------------------------------
@router.get("/storages")
async def list_storages(sess: AsyncSession = Depends(db_session), user=Depends(viewer)):
    rows = (await sess.execute(select(Storage).order_by(Storage.name))).scalars().all()
    return [_dump(r) for r in rows]


@router.post("/storages", status_code=201)
async def add_storage(request: Request, payload: StorageIn,
                      sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = Storage(**payload.model_dump())
    sess.add(obj)
    await sess.flush()
    await audit(sess, user.username, "storage_create", "storage", obj.id, payload.model_dump(),
                request.client.host if request.client else None)
    await sess.commit()
    await request.app.state.registry.refresh(sess)
    return _dump(obj)


@router.patch("/storages/{sid}")
async def patch_storage(request: Request, sid: uuid.UUID, patch: dict[str, Any] = Body(...),
                        sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = await _get(sess, Storage, sid)
    for k, v in patch.items():
        if k in Storage.__table__.columns and k not in ("id", "created_at"):
            setattr(obj, k, v)
    await audit(sess, user.username, "storage_update", "storage", sid, patch)
    await sess.commit()
    await request.app.state.registry.refresh(sess)
    return _dump(obj)


@router.delete("/storages/{sid}", status_code=204)
async def del_storage(sid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    obj = await _get(sess, Storage, sid)
    await sess.delete(obj)
    await audit(sess, user.username, "storage_delete", "storage", sid)
    await sess.commit()


@router.post("/storages/{sid}/test")
async def check_storage(sid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = await _get(sess, Storage, sid)
    return await test_storage(obj)


# ------------------------------ watch folders ------------------------------
@router.get("/watch-folders")
async def list_folders(sess: AsyncSession = Depends(db_session), user=Depends(viewer)):
    rows = (await sess.execute(select(WatchFolder).order_by(WatchFolder.name))).scalars().all()
    return [_dump(r) for r in rows]


@router.post("/watch-folders", status_code=201)
async def add_folder(request: Request, payload: WatchFolderIn,
                     sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = WatchFolder(**payload.model_dump())
    sess.add(obj)
    await sess.flush()
    await audit(sess, user.username, "folder_create", "watch_folder", obj.id, payload.model_dump())
    await sess.commit()
    return _dump(obj)


@router.patch("/watch-folders/{fid}")
async def patch_folder(fid: uuid.UUID, patch: dict[str, Any] = Body(...),
                       sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = await _get(sess, WatchFolder, fid)
    for k, v in patch.items():
        if k in WatchFolder.__table__.columns and k not in ("id", "created_at"):
            setattr(obj, k, v)
    await audit(sess, user.username, "folder_update", "watch_folder", fid, patch)
    await sess.commit()
    return _dump(obj)


@router.delete("/watch-folders/{fid}", status_code=204)
async def del_folder(fid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    obj = await _get(sess, WatchFolder, fid)
    await sess.delete(obj)
    await audit(sess, user.username, "folder_delete", "watch_folder", fid)
    await sess.commit()


@router.post("/watch-folders/{fid}/scan-now")
async def scan_now(fid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    await _get(sess, WatchFolder, fid)
    await sess.execute(text("UPDATE watch_folder SET scan_requested_at=now() WHERE id=:i"),
                       {"i": fid})
    await audit(sess, user.username, "folder_scan_now", "watch_folder", fid)
    await sess.commit()
    return {"requested": True}


@router.get("/watch-folders/{fid}/dry-run")
async def dry_run(request: Request, fid: uuid.UUID, limit: int = 20,
                  sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = await _get(sess, WatchFolder, fid)
    await request.app.state.registry.ensure_fresh(sess)
    try:
        return await dry_run_folder(sess, obj, request.app.state.registry, limit)
    except MFError as e:
        raise HTTPException(400, e.message) from e


# --------------------------------- presets ---------------------------------
@router.get("/presets")
async def list_presets(sess: AsyncSession = Depends(db_session), user=Depends(viewer)):
    rows = (await sess.execute(select(Preset).order_by(Preset.name, Preset.version.desc()))).scalars().all()
    return [_dump(r) for r in rows]


@router.post("/presets", status_code=201)
async def add_preset(payload: dict[str, Any] = Body(...),
                     sess: AsyncSession = Depends(db_session), user=Depends(op)):
    try:
        spec = validate_spec(payload["spec"] if "spec" in payload else payload)
    except (ConfigInvalidError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    obj = Preset(name=spec.name, version=spec.version, description=spec.description,
                 spec=spec.to_db(), enabled=True)
    sess.add(obj)
    try:
        await sess.flush()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, f"пресет {spec.name} v{spec.version} уже существует") from e
    await audit(sess, user.username, "preset_create", "preset", obj.id, {"name": spec.name})
    await sess.commit()
    return _dump(obj)


@router.post("/presets/validate")
async def validate(payload: dict[str, Any] = Body(...),
                   sess: AsyncSession = Depends(db_session), user=Depends(op)):
    try:
        return await validate_preset(sess, payload.get("spec", payload))
    except ConfigInvalidError as e:
        return {"ok": False, "error": e.message, "details": e.details}


@router.post("/presets/seed")
async def seed(sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    created = await seed_presets(sess)
    await audit(sess, user.username, "preset_seed", data={"created": created})
    await sess.commit()
    return {"created": created}


@router.patch("/presets/{pid}")
async def patch_preset(pid: uuid.UUID, patch: dict[str, Any] = Body(...),
                       sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = await _get(sess, Preset, pid)
    if "spec" in patch:
        obj.spec = validate_spec(patch["spec"]).to_db()
    if "enabled" in patch:
        obj.enabled = bool(patch["enabled"])
    if "description" in patch:
        obj.description = patch["description"]
    await audit(sess, user.username, "preset_update", "preset", pid)
    await sess.commit()
    return _dump(obj)


# ---------------------------------- assets ---------------------------------
@router.get("/assets")
async def list_assets(sess: AsyncSession = Depends(db_session), user=Depends(viewer),
                      state: str | None = None, wf: uuid.UUID | None = None,
                      q_: str = Query(None, alias="q"), limit: int = 100, offset: int = 0):
    stmt = select(Asset).order_by(Asset.discovered_at.desc()).limit(min(limit, 500)).offset(offset)
    if state:
        stmt = stmt.where(Asset.state == state)
    if wf:
        stmt = stmt.where(Asset.watch_folder_id == wf)
    if q_:
        stmt = stmt.where(Asset.uri.ilike(f"%{q_}%"))
    rows = (await sess.execute(stmt)).scalars().all()
    return [_dump(r) for r in rows]


# ----------------------------------- jobs ----------------------------------
@router.get("/jobs")
async def list_jobs(sess: AsyncSession = Depends(db_session), user=Depends(viewer),
                    state: str | None = None, wf: uuid.UUID | None = None,
                    limit: int = 100, offset: int = 0):
    stmt = select(Job).order_by(Job.created_at.desc()).limit(min(limit, 500)).offset(offset)
    if state:
        stmt = stmt.where(Job.state == state)
    if wf:
        stmt = stmt.where(Job.watch_folder_id == wf)
    rows = (await sess.execute(stmt)).scalars().all()
    return [_dump(r) for r in rows]


@router.post("/jobs", status_code=201)
async def add_job(request: Request, payload: JobCreate,
                  sess: AsyncSession = Depends(db_session), user=Depends(op)):
    await request.app.state.registry.ensure_fresh(sess)
    try:
        job_id, created, out = await create_job(sess, payload, user.username,
                                                request.app.state.registry)
    except ConfigInvalidError as e:
        raise HTTPException(400, e.message) from e
    await audit(sess, user.username, "job_create", "job", job_id,
                {"created": created, "output": out})
    await sess.commit()
    return {"id": job_id, "created": created, "output": out}


@router.get("/jobs/{jid}")
async def get_job(jid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(viewer)):
    job = await _get(sess, Job, jid)
    steps = (await sess.execute(select(JobStep).where(JobStep.job_id == jid)
                                .order_by(JobStep.idx))).scalars().all()
    events = (await sess.execute(select(JobEvent).where(JobEvent.job_id == jid)
                                 .order_by(JobEvent.ts.desc()).limit(200))).scalars().all()
    arts = (await sess.execute(select(Artifact).where(Artifact.job_id == jid))).scalars().all()
    return {"job": _dump(job), "steps": [_dump(s) for s in steps],
            "events": [_dump(e) for e in events], "artifacts": [_dump(a) for a in arts]}


@router.post("/jobs/{jid}/cancel")
async def cancel_job(jid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    state = await q.cancel(sess, jid)
    await audit(sess, user.username, "job_cancel", "job", jid, {"state": state})
    await sess.commit()
    if state is None:
        raise HTTPException(409, "задача уже завершена")
    return {"state": state}


@router.post("/jobs/{jid}/retry")
async def retry_job(jid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    ok = await q.requeue_manual(sess, jid)
    await audit(sess, user.username, "job_retry", "job", jid)
    await sess.commit()
    if not ok:
        raise HTTPException(409, "перезапуск возможен только для failed/cancelled")
    return {"state": "queued"}


@router.patch("/jobs/{jid}")
async def patch_job(jid: uuid.UUID, patch: dict[str, Any] = Body(...),
                    sess: AsyncSession = Depends(db_session), user=Depends(op)):
    job = await _get(sess, Job, jid)
    if "priority" in patch:
        job.priority = max(0, min(1000, int(patch["priority"])))
    if "max_attempts" in patch:
        job.max_attempts = max(1, int(patch["max_attempts"]))
    await audit(sess, user.username, "job_update", "job", jid, patch)
    await sess.commit()
    return _dump(job)


@router.get("/jobs/{jid}/steps/{step_name}/log")
async def get_step_log(
        request: Request,
        jid: uuid.UUID,
        step_name: str,
        sess: AsyncSession = Depends(db_session),
        user=Depends(viewer)
):
    """Возвращает текст лог-файла для конкретного шага задачи."""
    # 1. Проверяем существование задачи (для прав доступа)
    await _get(sess, Job, jid)

    # 2. Ищем шаг и его log_uri
    step = (await sess.execute(
        select(JobStep).where(JobStep.job_id == jid, JobStep.name == step_name)
    )).scalar_one_or_none()

    if not step or not step.log_uri:
        raise HTTPException(404, "Лог для этого шага не найден или ещё не создан")

    # 3. Читаем лог из хранилища
    try:
        await request.app.state.registry.ensure_fresh(sess)
        be, rel = request.app.state.registry.resolve(step.log_uri)

        # Ограничиваем размер читаемого лога (например, 5 МБ), чтобы не перегружать память UI
        MAX_LOG_SIZE = 5 * 1024 * 1024
        content_bytes = await be.read_range(rel, 0, MAX_LOG_SIZE)

        # Декодируем с заменой некорректных символов
        content_text = content_bytes.decode("utf-8", errors="replace")

        # Если лог был обрезан, добавим уведомление
        if len(content_bytes) == MAX_LOG_SIZE:
            content_text += "\n\n--- [ЛОГ ОБРЕЗАН: превышен лимит 5 МБ] ---"

        return Response(content=content_text, media_type="text/plain; charset=utf-8")

    except Exception as e:
        log.error("log_read_failed", job_id=str(jid), step=step_name, error=str(e))
        raise HTTPException(500, f"Ошибка чтения лога из хранилища: {e}")

# -------------------------------- schedules --------------------------------
@router.get("/schedules")
async def list_schedules(sess: AsyncSession = Depends(db_session), user=Depends(viewer)):
    rows = (await sess.execute(select(Schedule).order_by(Schedule.name))).scalars().all()
    return [_dump(r) for r in rows]


@router.post("/schedules", status_code=201)
async def add_schedule(payload: ScheduleIn, sess: AsyncSession = Depends(db_session),
                       user=Depends(op)):
    obj = Schedule(**payload.model_dump())
    sess.add(obj)
    await sess.flush()
    await audit(sess, user.username, "schedule_create", "schedule", obj.id, payload.model_dump())
    await sess.commit()
    return _dump(obj)


@router.patch("/schedules/{sid}")
async def patch_schedule(sid: uuid.UUID, patch: dict[str, Any] = Body(...),
                         sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = await _get(sess, Schedule, sid)
    for k, v in patch.items():
        if k in Schedule.__table__.columns and k not in ("id", "created_at"):
            setattr(obj, k, v)
    await audit(sess, user.username, "schedule_update", "schedule", sid, patch)
    await sess.commit()
    return _dump(obj)


@router.delete("/schedules/{sid}", status_code=204)
async def del_schedule(sid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = await _get(sess, Schedule, sid)
    await sess.delete(obj)
    await audit(sess, user.username, "schedule_delete", "schedule", sid)
    await sess.commit()


@router.post("/schedules/preview")
async def preview_cron(payload: dict[str, Any] = Body(...), user=Depends(viewer)):
    from croniter import croniter
    cron, tz, count = payload.get("cron", ""), payload.get("timezone", "UTC"), int(payload.get("count", 5))
    if not croniter.is_valid(cron):
        raise HTTPException(400, "некорректный cron")
    import zoneinfo
    base = dt.datetime.now(zoneinfo.ZoneInfo(tz))
    it = croniter(cron, base)
    return {"next": [it.get_next(dt.datetime).isoformat() for _ in range(min(count, 20))]}


# --------------------------------- workers ---------------------------------
@router.get("/workers")
async def list_workers(sess: AsyncSession = Depends(db_session), user=Depends(viewer)):
    rows = (await sess.execute(select(Worker).order_by(Worker.id))).scalars().all()
    return [_dump(r) for r in rows]


@router.post("/workers/{wid}/drain")
async def drain_worker(wid: str, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    res = await sess.execute(text(
        "UPDATE worker SET state='draining', updated_at=now() WHERE id=:i RETURNING id"), {"i": wid})
    if res.first() is None:
        raise HTTPException(404, "воркер не найден")
    await audit(sess, user.username, "worker_drain", "worker", wid)
    await sess.commit()
    return {"state": "draining"}


# ---------------------------------- audit ----------------------------------
@router.get("/audit")
async def list_audit(sess: AsyncSession = Depends(db_session), user=Depends(admin),
                     limit: int = 200):
    rows = (await sess.execute(select(AuditLog).order_by(AuditLog.ts.desc())
                               .limit(min(limit, 1000)))).scalars().all()
    return [_dump(r) for r in rows]