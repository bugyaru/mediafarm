from __future__ import annotations

import json
import time
import uuid
from html import escape
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from mf_core import queue as q
from mf_core.errors import ConfigInvalidError
from mf_core.media.presets import BUILTIN_PRESETS, seed_presets, validate_spec
from mf_core.models import (
    AppUser, Artifact, Asset, Job, JobEvent, JobStep, Preset, Schedule, Storage, WatchFolder, Worker,
)
from mf_core.schemas import JobCreate
from mf_core.util import utcnow
from ui.deps import current_user, db_session, parse_form, render, require_role, templates
from ui.security import create_user, hash_password
from ui.services import audit, create_job, dry_run_folder, test_storage, validate_preset

router = APIRouter()
op = require_role("operator")
admin = require_role("admin")

def back(request: Request, path: str, msg: str = "", err: str = "") -> Response:
    """PRG для обычных форм; для HTMX — HX-Redirect (обрабатывается даже при hx-swap='none')."""
    from urllib.parse import urlencode
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    url = f"{path}?{query}" if query else path
    if request.headers.get("hx-request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)

# ------------------------- хранилища: статусы проверок -------------------------

def _st_tests(request: Request) -> dict:
    """Последние результаты проверок в памяти процесса: {storage_id: {ok, latency_ms, checked_at}}."""
    if not hasattr(request.app.state, "storage_tests"):
        request.app.state.storage_tests = {}
    return request.app.state.storage_tests

def _st_flash(kind: str, text: str) -> str:
    return (f'<div id="st-flash" hx-swap-oob="outerHTML" class="st-flash {kind}" role="status">'
            f'{"✓" if kind == "ok" else "✕"} {escape(text)}</div>')

async def _st_table(request: Request, sess: AsyncSession, oob: bool = True) -> str:
    rows = (await sess.execute(select(Storage).order_by(Storage.name))).scalars().all()
    return templates.get_template("_storages_table.html").render(
        storages=rows, tests=_st_tests(request), oob=oob)

def _storage_error_hint(err: str) -> str:
    e = err.lower()
    if "нет хранилища для uri" in e:
        return "проверьте base_uri: запись хранилища должна покрывать этот URI"
    if "некорректный s3 uri" in e:
        return "в base_uri нужен бакет: s3://имя-бакета"
    if "nosuchbucket" in e:
        return "бакет не существует — проверьте имя в base_uri"
    if any(s in e for s in ("invalidaccesskeyid", "signaturedoesnotmatch", "access denied", "403")):
        return "проверьте access_key/secret_key в secret_ref"
    if "connection refused" in e or "connect call failed" in e:
        return "endpoint недоступен — проверьте endpoint_url и порт в options"
    if "getaddrinfo" in e or "name or service not known" in e or "nodename nor servname" in e:
        return "DNS не резолвит хост — проверьте endpoint_url"
    if "timeout" in e or "timed out" in e:
        return "таймаут — проверьте сеть и фаервол до хранилища"
    if any(s in e for s in ("no such file", "not a directory", "permission denied")):
        return "проверьте mount_path и права доступа"
    return ""

async def _stats_ctx(sess: AsyncSession) -> dict:
    rows = await q.stats(sess)
    workers = (await sess.execute(select(Worker))).scalars().all()
    return {
        "queue": rows,
        "queued": sum(r["n"] for r in rows if r["state"] == "queued"),
        "running": sum(r["n"] for r in rows if r["state"] == "running"),
        "failed": sum(r["n"] for r in rows if r["state"] == "failed"),
        "workers": workers,
        "assets": await sess.scalar(select(func.count()).select_from(Asset)),
    }

@router.get("/")
async def dashboard(request: Request, sess: AsyncSession = Depends(db_session),
                    user=Depends(require_role("viewer"))):
    ctx = await _stats_ctx(sess)
    recent = (await sess.execute(select(Job).where(Job.state == "failed")
                                 .order_by(Job.created_at.desc()).limit(10))).scalars().all()
    return render(request, "dashboard.html", user, recent=recent, **ctx)

@router.get("/partials/stats")
async def partial_stats(request: Request, sess: AsyncSession = Depends(db_session),
                        user=Depends(require_role("viewer"))):
    return render(request, "_stats.html", user, **await _stats_ctx(sess))

@router.get("/jobs")
async def jobs_page(request: Request, state: str = "", sess: AsyncSession = Depends(db_session),
                    user=Depends(require_role("viewer"))):
    stmt = select(Job).order_by(Job.created_at.desc()).limit(200)
    if state:
        stmt = stmt.where(Job.state == state)
    jobs = (await sess.execute(stmt)).scalars().all()
    presets = (await sess.execute(select(Preset).where(Preset.enabled)
                                  .order_by(Preset.name))).scalars().all()
    return render(request, "jobs.html", user, jobs=jobs, state=state, presets=presets)

@router.get("/jobs/{jid}")
async def job_page(request: Request, jid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                   user=Depends(require_role("viewer"))):
    job = await sess.get(Job, jid)
    if job is None:
        raise HTTPException(404, "задача не найдена")
    steps = (await sess.execute(select(JobStep).where(JobStep.job_id == jid)
                                .order_by(JobStep.idx))).scalars().all()
    events = (await sess.execute(select(JobEvent).where(JobEvent.job_id == jid)
                                 .order_by(JobEvent.ts.desc()).limit(100))).scalars().all()
    arts = (await sess.execute(select(Artifact).where(Artifact.job_id == jid))).scalars().all()
    asset = await sess.get(Asset, job.asset_id) if job.asset_id else None
    return render(request, "job_detail.html", user, job=job, steps=steps, events=events,
                  artifacts=arts, asset=asset)

@router.post("/jobs")
async def jobs_create(request: Request, sess: AsyncSession = Depends(db_session),
                      user=Depends(op)):
    form = parse_form(await request.form(), json_fields=("params",), int_fields=("priority",))
    try:
        payload = JobCreate(
            kind=form.get("kind", "transcode"), url=form.get("url") or None,
            input_uri=form.get("input_uri") or None,
            asset_id=uuid.UUID(form["asset_id"]) if form.get("asset_id") else None,
            preset_name=form.get("preset_name") or None,
            priority=int(form.get("priority", 100)), params=form.get("params") or {},
        )
        await request.app.state.registry.ensure_fresh(sess)
        jid, created, out = await create_job(sess, payload, user.username,
                                             request.app.state.registry)
        await audit(sess, user.username, "job_create", "job", jid, {"output": out})
        await sess.commit()
    except (ConfigInvalidError, ValueError, KeyError) as e:
        return back(request, "/jobs", err=str(e))
    return back(request, f"/jobs/{jid}", msg="Задача создана" if created else "Такая задача уже есть")

@router.post("/jobs/{jid}/cancel")
async def jobs_cancel(request: Request, jid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                      user=Depends(op)):
    await q.cancel(sess, jid)
    await audit(sess, user.username, "job_cancel", "job", jid)
    await sess.commit()
    return back(request, f"/jobs/{jid}", msg="Отмена запрошена")

@router.post("/jobs/{jid}/retry")
async def jobs_retry(request: Request, jid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                     user=Depends(op)):
    ok = await q.requeue_manual(sess, jid)
    await audit(sess, user.username, "job_retry", "job", jid)
    await sess.commit()
    return back(request, f"/jobs/{jid}", msg="Перезапущена" if ok else "",
                err="" if ok else "Нельзя перезапустить")

# --------------------------------- storages ---------------------------------

@router.get("/storages")
async def storages_page(request: Request, sess: AsyncSession = Depends(db_session),
                        user=Depends(require_role("viewer"))):
    rows = (await sess.execute(select(Storage).order_by(Storage.name))).scalars().all()
    return render(request, "storages.html", user, storages=rows, tests=_st_tests(request))

@router.post("/storages")
async def storages_create(request: Request, sess: AsyncSession = Depends(db_session),
                          user=Depends(op)):
    try:
        data = parse_form(await request.form(), json_fields=("options",),
                          bool_fields=("read_only", "enabled"))
        data.setdefault("enabled", True)
        for k in ("mount_path", "secret_ref"):  # пустые строки -> NULL
            if data.get(k) == "":
                data[k] = None
        if await sess.scalar(select(func.count()).select_from(Storage)
                                     .where(Storage.name == data.get("name"))):
            return HTMLResponse(_st_flash("err", f"имя «{data.get('name')}» уже занято"))
        obj = Storage(**{k: v for k, v in data.items() if k in Storage.__table__.columns})
        sess.add(obj)
        await sess.flush()
        await audit(sess, user.username, "storage_create", "storage", obj.id, {"name": obj.name})
        await sess.commit()
        await request.app.state.registry.refresh(sess)
    except HTTPException as e:
        return HTMLResponse(_st_flash("err", str(e.detail)))
    except Exception as e:  # noqa: BLE001
        await sess.rollback()
        return HTMLResponse(_st_flash("err", f"не удалось создать: {e}"))
    return HTMLResponse(await _st_table(request, sess)
                        + _st_flash("ok", f"Хранилище «{obj.name}» добавлено"))

@router.post("/storages/{sid}/test")
async def storages_test(request: Request, sid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                        user=Depends(op)):
    obj = await sess.get(Storage, sid)
    if obj is None:
        raise HTTPException(404, "хранилище не найдено")
    t0 = time.perf_counter()
    try:
        result = await test_storage(obj)
        if not isinstance(result, dict):
            result = {"ok": bool(result)}
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    result.setdefault("ok", False)
    result.setdefault("latency_ms", round((time.perf_counter() - t0) * 1000, 1))
    result["checked_at"] = utcnow().strftime("%H:%M:%S UTC")
    err_text = str(result.get("error") or result.get("message") or result.get("detail") or "")
    if not result["ok"] and not err_text:
        err_text = "хранилище вернуло отрицательный результат без описания"
    extras = [(k, v) for k, v in result.items()
              if k not in ("ok", "error", "message", "detail", "latency_ms", "checked_at")
              and isinstance(v, (str, int, float)) and not isinstance(v, bool)][:4]
    hint = _storage_error_hint(err_text) if not result["ok"] else ""
    _st_tests(request)[str(sid)] = {k: result.get(k) for k in ("ok", "latency_ms", "checked_at")}
    await audit(sess, user.username, "storage_test", "storage", sid,
                {"ok": bool(result["ok"]), "latency_ms": result["latency_ms"]})
    await sess.commit()
    resp = render(request, "_storage_test.html", user, result=result, storage=obj,
                  extras=extras, err_text=err_text, hint=hint)
    resp.headers["HX-Trigger"] = json.dumps({"storageTested": {
        "id": str(sid), "ok": bool(result["ok"]),
        "latency_ms": result["latency_ms"], "checked_at": result["checked_at"]}})
    return resp

@router.post("/storages/{sid}/delete")
async def storages_delete(request: Request, sid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                          user=Depends(admin)):
    obj = await sess.get(Storage, sid)
    name = obj.name if obj else ""
    if obj:
        try:
            await sess.delete(obj)
            await audit(sess, user.username, "storage_delete", "storage", sid)
            await sess.commit()
            await request.app.state.registry.refresh(sess)
            _st_tests(request).pop(str(sid), None)
        except Exception as e:  # noqa: BLE001
            await sess.rollback()
            return HTMLResponse(_st_flash("err", f"не удалось удалить «{name}»: {e}"))
    return HTMLResponse(await _st_table(request, sess)
                        + _st_flash("ok", f"Хранилище «{name}» удалено" if name else "Хранилище уже удалено"))

@router.post("/storages/{sid}/toggle")
async def storages_toggle(request: Request, sid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = await sess.get(Storage, sid)
    if obj:
        obj.enabled = not obj.enabled
        await audit(sess, user.username, "storage_toggle", "storage", sid, {"enabled": obj.enabled})
        await sess.commit()
        await request.app.state.registry.refresh(sess)
        return templates.TemplateResponse(request, "_storage_row.html", {"s": obj, "tests": _st_tests(request)})

@router.get("/storages/{sid}/edit")
async def storages_edit_form(request: Request, sid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                             user=Depends(op)):
    obj = await sess.get(Storage, sid)
    if not obj:
        raise HTTPException(404, "Хранилище не найдено")
    return render(request, "_storage_edit_form.html", user, storage=obj)

@router.post("/storages/{sid}/edit")
async def storages_edit_save(request: Request, sid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                             user=Depends(op)):
    obj = await sess.get(Storage, sid)
    if not obj:
        raise HTTPException(404, "Хранилище не найдено")
    data = parse_form(await request.form(), json_fields=("options",), bool_fields=("read_only", "enabled"))
    # Нормализация пустых строк в None
    for k in ("mount_path", "secret_ref"):
        if data.get(k) == "":
            data[k] = None
    for k, v in data.items():
        if k in Storage.__table__.columns and k not in ("id", "created_at", "updated_at"):
            setattr(obj, k, v)
    await audit(sess, user.username, "storage_update", "storage", sid, data)
    await sess.commit()
    await request.app.state.registry.refresh(sess)
    return HTMLResponse(
        f'<div hx-swap-oob="true" id="st-flash" class="st-flash ok">✓ Хранилище «{obj.name}» обновлено</div>'
        f'<script>document.querySelector("[data-drawer-close]").click(); '
        f'htmx.ajax("GET", "/storages/{sid}/row", {{target: "#row-{sid}", swap: "outerHTML"}});</script>'
    )

@router.get("/storages/{sid}/row")
async def storages_row(request: Request, sid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                       user=Depends(require_role("viewer"))):
    obj = await sess.get(Storage, sid)
    if not obj:
        return HTMLResponse('<tr><td colspan="6" class="mf-empty">Хранилище удалено</td></tr>')
    return templates.TemplateResponse(request, "_storage_row.html", {"s": obj, "tests": _st_tests(request)})

# --------------------------------- folders ---------------------------------

@router.get("/folders")
async def folders_page(request: Request, sess: AsyncSession = Depends(db_session),
                       user=Depends(require_role("viewer"))):
    folders = (await sess.execute(select(WatchFolder).order_by(WatchFolder.name))).scalars().all()
    storages = (await sess.execute(select(Storage).order_by(Storage.name))).scalars().all()
    presets = (await sess.execute(select(Preset).order_by(Preset.name))).scalars().all()
    from mf_core.parsers import available
    return render(request, "folders.html", user, folders=folders, storages=storages,
                  presets=presets, parsers=available())

@router.post("/folders")
async def folders_create(request: Request, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    data = parse_form(await request.form(),
                      json_fields=("include", "exclude", "parser_opts", "job_template"),
                      int_fields=("poll_interval_s", "stability_s", "max_concurrency"),
                      bool_fields=("recursive", "enabled"))
    try:
        if data.get("storage_id"):
            data["storage_id"] = uuid.UUID(data["storage_id"])
        if data.get("preset_id"):
            data["preset_id"] = uuid.UUID(data["preset_id"])
        else:
            data.pop("preset_id", None)
        obj = WatchFolder(**{k: v for k, v in data.items() if k in WatchFolder.__table__.columns})
        sess.add(obj)
        await sess.flush()
        await audit(sess, user.username, "folder_create", "watch_folder", obj.id, {"name": obj.name})
        await sess.commit()
    except Exception as e:  # noqa: BLE001
        return back(request, "/folders", err=str(e))
    return back(request, "/folders", msg="Папка добавлена")

@router.post("/folders/{fid}/scan")
async def folders_scan(request: Request, fid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                       user=Depends(op)):
    await sess.execute(text("UPDATE watch_folder SET scan_requested_at=now() WHERE id=:i"), {"i": fid})
    await audit(sess, user.username, "folder_scan_now", "watch_folder", fid)
    await sess.commit()
    return back(request, "/folders", msg="Скан запрошен")

@router.post("/folders/{fid}/toggle")
async def folders_toggle(request: Request, fid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                         user=Depends(op)):
    obj = await sess.get(WatchFolder, fid)
    if obj:
        obj.enabled = not obj.enabled
        await audit(sess, user.username, "folder_toggle", "watch_folder", fid, {"enabled": obj.enabled})
        await sess.commit()
    return back(request, "/folders", msg="Изменено")

@router.get("/folders/{fid}/dry-run")
async def folders_dry_run(request: Request, fid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                          user=Depends(op)):
    obj = await sess.get(WatchFolder, fid)
    if obj is None:
        raise HTTPException(404)
    await request.app.state.registry.ensure_fresh(sess)
    try:
        result = await dry_run_folder(sess, obj, request.app.state.registry, 20)
    except Exception as e:  # noqa: BLE001
        result = {"error": str(e), "items": [], "sources": []}
    return render(request, "_dry_run.html", user, result=result, folder=obj)

# --------------------------------- presets ---------------------------------

@router.get("/presets")
async def presets_page(request: Request, sess: AsyncSession = Depends(db_session),
                       user=Depends(require_role("viewer"))):
    rows = (await sess.execute(select(Preset).order_by(Preset.name, Preset.version.desc()))).scalars().all()
    return render(request, "presets.html", user, presets=rows, builtins=sorted(BUILTIN_PRESETS))

@router.post("/presets")
async def presets_create(request: Request, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    form = await request.form()
    try:
        spec = validate_spec(json.loads(form.get("spec") or "{}"))
        obj = Preset(name=spec.name, version=spec.version, description=spec.description,
                     spec=spec.to_db(), enabled=True)
        sess.add(obj)
        await sess.flush()
        await audit(sess, user.username, "preset_create", "preset", obj.id, {"name": spec.name})
        await sess.commit()
    except Exception as e:  # noqa: BLE001
        return back(request, "/presets", err=str(e))
    return back(request, "/presets", msg="Пресет сохранён")

@router.post("/presets/validate")
async def presets_validate(request: Request, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    form = await request.form()
    try:
        result = await validate_preset(sess, json.loads(form.get("spec") or "{}"))
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": str(e)}
    return render(request, "_preset_validation.html", user, result=result)

@router.post("/presets/seed")
async def presets_seed(request: Request, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    created = await seed_presets(sess)
    await audit(sess, user.username, "preset_seed", data={"created": created})
    await sess.commit()
    return back(request, "/presets", msg=f"Добавлено пресетов: {created}")

@router.post("/presets/{pid}/toggle")
async def presets_toggle(request: Request, pid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    obj = await sess.get(Preset, pid)
    if obj:
        obj.enabled = not obj.enabled
        await audit(sess, user.username, "preset_toggle", "preset", pid, {"enabled": obj.enabled})
        await sess.commit()
    return back(request, "/presets", msg="Статус пресета изменён")

# -------------------------------- schedules --------------------------------

@router.get("/schedules")
async def schedules_page(request: Request, sess: AsyncSession = Depends(db_session),
                         user=Depends(require_role("viewer"))):
    rows = (await sess.execute(select(Schedule).order_by(Schedule.name))).scalars().all()
    presets = (await sess.execute(select(Preset).order_by(Preset.name))).scalars().all()
    return render(request, "schedules.html", user, schedules=rows, presets=presets,
                  leader=request.app.state.control.leader)

@router.post("/schedules")
async def schedules_create(request: Request, sess: AsyncSession = Depends(db_session), user=Depends(op)):
    data = parse_form(await request.form(), json_fields=("job_template",),
                      int_fields=("misfire_grace_s",), bool_fields=("enabled",))
    try:
        obj = Schedule(**{k: v for k, v in data.items() if k in Schedule.__table__.columns})
        sess.add(obj)
        await sess.flush()
        await audit(sess, user.username, "schedule_create", "schedule", obj.id, {"name": obj.name})
        await sess.commit()
    except Exception as e:  # noqa: BLE001
        return back(request, "/schedules", err=str(e))
    return back(request, "/schedules", msg="Расписание сохранено")

@router.post("/schedules/{sid}/toggle")
async def schedules_toggle(request: Request, sid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                           user=Depends(op)):
    obj = await sess.get(Schedule, sid)
    if obj:
        obj.enabled = not obj.enabled
        await audit(sess, user.username, "schedule_toggle", "schedule", sid, {"enabled": obj.enabled})
        await sess.commit()
    return back(request, "/schedules", msg="Изменено")

# --------------------------------- workers ---------------------------------

@router.get("/workers")
async def workers_page(request: Request, sess: AsyncSession = Depends(db_session),
                       user=Depends(require_role("viewer"))):
    rows = (await sess.execute(select(Worker).order_by(Worker.id))).scalars().all()
    return render(request, "workers.html", user, workers=rows)

@router.post("/workers/{wid}/drain")
async def workers_drain(request: Request, wid: str, sess: AsyncSession = Depends(db_session),
                        user=Depends(op)):
    await sess.execute(text("UPDATE worker SET state='draining' WHERE id=:i"), {"i": wid})
    await audit(sess, user.username, "worker_drain", "worker", wid)
    await sess.commit()
    return back(request, "/workers", msg="Воркер переводится в drain")

# ---------------------------------- assets ---------------------------------

@router.get("/assets")
async def assets_page(request: Request, state: str = "", q: str = "",
                      sess: AsyncSession = Depends(db_session), user=Depends(require_role("viewer"))):
    stmt = select(Asset).order_by(Asset.discovered_at.desc()).limit(200)
    if state:
        stmt = stmt.where(Asset.state == state)
    if q:
        stmt = stmt.where(Asset.uri.ilike(f"%{q}%"))
    assets = (await sess.execute(stmt)).scalars().all()
    presets = (await sess.execute(select(Preset).where(Preset.enabled))).scalars().all()
    return render(request, "assets.html", user, assets=assets, presets=presets, state=state, q=q)

# ---------------------------------- users ----------------------------------

@router.get("/users")
async def users_page(request: Request, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    rows = (await sess.execute(select(AppUser).order_by(AppUser.username))).scalars().all()
    return render(request, "users.html", user, users=rows)

@router.post("/users")
async def users_create(request: Request, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    form = await request.form()
    try:
        new = await create_user(sess, form["username"], form.get("password") or None,
                                form.get("role", "viewer"))
        await audit(sess, user.username, "user_create", "user", new.id, {"role": new.role})
        await sess.commit()
    except Exception as e:  # noqa: BLE001
        return back(request, "/users", err=str(e))
    return back(request, "/users", msg="Пользователь создан")

@router.post("/users/{uid}/toggle")
async def users_toggle(request: Request, uid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                       user=Depends(admin)):
    obj = await sess.get(AppUser, uid)
    if obj and obj.id != user.id:
        obj.is_active = not obj.is_active
        await audit(sess, user.username, "user_toggle", "user", uid, {"active": obj.is_active})
        await sess.commit()
    return back(request, "/users", msg="Изменено")

# --- Удаление WatchFolder (6 колонок) ---

@router.post("/folders/{fid}/delete")
async def folders_delete(fid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    obj = await sess.get(WatchFolder, fid)
    if obj:
        await sess.delete(obj)
        await audit(sess, user.username, "folder_delete", "watch_folder", fid)
        await sess.commit()
    return HTMLResponse('<tr><td colspan="6" style="color: #16a34a; text-align: center; padding: 1rem;">✓ Папка наблюдения удалена</td></tr>')

# --- Удаление Preset (6 колонок) ---

@router.post("/presets/{pid}/delete")
async def presets_delete(pid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    obj = await sess.get(Preset, pid)
    if obj:
        await sess.delete(obj)
        await audit(sess, user.username, "preset_delete", "preset", pid)
        await sess.commit()
    return HTMLResponse('<tr><td colspan="6" style="color: #16a34a; text-align: center; padding: 1rem;">✓ Пресет удалён</td></tr>')

# --- Удаление Schedule (7 колонок) ---

@router.post("/schedules/{sid}/delete")
async def schedules_delete(sid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    obj = await sess.get(Schedule, sid)
    if obj:
        await sess.delete(obj)
        await audit(sess, user.username, "schedule_delete", "schedule", sid)
        await sess.commit()
    return HTMLResponse('<tr><td colspan="7" style="color: #16a34a; text-align: center; padding: 1rem;">✓ Расписание удалено</td></tr>')

# --- Удаление AppUser (5 колонок) ---

@router.post("/users/{uid}/delete")
async def users_delete(uid: uuid.UUID, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    obj = await sess.get(AppUser, uid)
    if obj and obj.id != user.id:
        await sess.delete(obj)
        await audit(sess, user.username, "user_delete", "user", uid)
        await sess.commit()
    return HTMLResponse('<tr><td colspan="5" style="color: #16a34a; text-align: center; padding: 1rem;">✓ Пользователь удалён</td></tr>')
    return HTMLResponse('<tr><td colspan="5" style="color: #dc2626; text-align: center; padding: 1rem;">✗ Нельзя удалить текущего пользователя</td></tr>')

# --- Удаление Worker ---

@router.post("/workers/{wid}/delete")
async def workers_delete(wid: str, sess: AsyncSession = Depends(db_session), user=Depends(admin)):
    obj = await sess.get(Worker, wid)
    if obj:
        await sess.delete(obj)
        await audit(sess, user.username, "worker_delete", "worker", wid)
        await sess.commit()
    return HTMLResponse('<tr><td colspan="9" style="color: #16a34a; text-align: center; padding: 1rem;">✓ Воркер удалён из реестра</td></tr>')