"""Полные логи инструментов в деталях задачи: REST (история/выгрузка) + WebSocket (live).

Кадр лога — строки job_log по курсору id. Воркер пишет пачками, триггер шлёт
NOTIFY job_logs, мост дочитывает новые строки и отдаёт их в браузер.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mf_core import joblog
from mf_core.db import get_sessionmaker
from mf_core.models import Job
from mf_core.telemetry import get_logger
from ui.deps import db_session, require_role
from ui.security import read_session_cookie

log = get_logger("mf.ui.logs")
router = APIRouter()
viewer = require_role("viewer")
TERMINAL = ("succeeded", "failed", "cancelled")

def _streams(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    out = [s.strip() for s in raw.split(",") if s.strip() in joblog.STREAMS]
    return out or None

def _flt(step: int | None, tool: str | None, stream: str | None, q: str | None) -> dict:
    return {"step": step, "tool": tool or None, "streams": _streams(stream), "search": q or None}

async def _job(sess: AsyncSession, jid: uuid.UUID) -> Job:
    job = await sess.get(Job, jid)
    if job is None:
        raise HTTPException(404, "задача не найдена")
    return job

# ------------------------------- REST --------------------------------------
@router.get("/jobs/{jid}/logs")
@router.get("/api/v1/jobs/{jid}/logs")
async def job_logs(
        jid: uuid.UUID, request: Request, after: int = 0, before: int = 0, limit: int = 0,
        tail: bool = False, step: int | None = None, tool: str | None = None,
        stream: str | None = None, q: str | None = None,
        sess: AsyncSession = Depends(db_session), user=Depends(viewer),
):
    s = request.app.state.settings
    job = await _job(sess, jid)
    lim = max(1, min(limit or s.ui_log_page_size, s.ui_log_max_page))
    flt = _flt(step, tool, stream, q)
    rows = (await joblog.tail(sess, jid, limit=lim, **flt) if tail and not after
            else await joblog.fetch(sess, jid, after=after, before=before or None,
                                    limit=lim, **flt))
    return {
        "job_id": str(jid), "state": job.state, "progress": job.progress,
        "lines": rows, "last_id": rows[-1]["id"] if rows else after,
        "has_more": len(rows) >= lim, "total": await joblog.totals(sess, jid),
    }

@router.get("/jobs/{jid}/logs/summary")
@router.get("/api/v1/jobs/{jid}/logs/summary")
async def job_logs_summary(jid: uuid.UUID, sess: AsyncSession = Depends(db_session),
                           user=Depends(viewer)):
    await _job(sess, jid)
    return {"job_id": str(jid), "groups": await joblog.summary(sess, jid),
            "total": await joblog.totals(sess, jid)}

@router.get("/jobs/{jid}/logs/raw")
@router.get("/api/v1/jobs/{jid}/logs/raw")
async def job_logs_raw(
        jid: uuid.UUID, download: bool = False, step: int | None = None, tool: str | None = None,
        stream: str | None = None, q: str | None = None,
        sess: AsyncSession = Depends(db_session), user=Depends(viewer),
):
    await _job(sess, jid)
    flt = _flt(step, tool, stream, q)

    async def gen():
        sm = get_sessionmaker()
        cursor = 0
        while True:
            async with sm() as s2:
                rows = await joblog.fetch(s2, jid, after=cursor, limit=2000, **flt)
            if not rows:
                return
            cursor = rows[-1]["id"]
            yield "".join(f"{r['ts']} [{r['step_idx']}:{r['tool']}/{r['stream']}] {r['line']}\n"
                          for r in rows)

    headers = ({"Content-Disposition": f'attachment; filename="job-{jid}.log"'}
               if download else {})
    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8", headers=headers)

# ------------------------------ WebSocket ----------------------------------
@router.websocket("/ws/jobs/{jid}/logs")
async def ws_job_logs(ws: WebSocket, jid: uuid.UUID):
    s = ws.app.state.settings
    claims = read_session_cookie(ws.cookies.get(s.ui_session_cookie), s)
    if claims is None:
        await ws.close(code=4401)
        return
    bus = ws.app.state.bus
    if bus.subscribers_of("job_logs") >= s.ui_log_ws_clients:
        await ws.close(code=4429)
        return
    await ws.accept()
    p = ws.query_params
    flt = _flt(int(p["step"]) if p.get("step") else None, p.get("tool"),
               p.get("stream"), p.get("q"))
    cursor = int(p.get("after") or 0)
    page = max(1, min(int(p.get("limit") or s.ui_log_page_size), s.ui_log_max_page))
    queue = bus.subscribe("job_logs")
    sm = get_sessionmaker()
    try:
        async with sm() as sess:
            job = await sess.get(Job, jid)
            if job is None:
                await ws.send_json({"type": "error", "message": "задача не найдена"})
                await ws.close(code=4404)
                return
            state = job.state
            first = await joblog.tail(sess, jid, limit=page, **flt) if cursor == 0 else []
        if first:
            cursor = first[-1]["id"]
        await ws.send_json({"type": "lines", "lines": first, "state": state, "cursor": cursor})

        last_state, idle, quiet = state, 0.0, 0
        while True:
            # NOTIFY — только «будильник»: строки всегда дочитываем из БД по курсору,
            # поэтому потеря уведомления не ломает поток.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(queue.get(), timeout=2.0)
            async with sm() as sess:
                rows = await joblog.fetch(sess, jid, after=cursor, limit=1000, **flt)
                state = await sess.scalar(text("SELECT state FROM job WHERE id=:i"), {"i": jid})
            if rows:
                cursor = rows[-1]["id"]
                await ws.send_json({"type": "lines", "lines": rows, "state": state,
                                    "cursor": cursor})
                idle, quiet = 0.0, 0
            elif state != last_state:
                await ws.send_json({"type": "state", "state": state})
                idle, quiet = 0.0, 0
            else:
                idle += 2.0
                if idle >= 20:
                    await ws.send_json({"type": "ping", "state": state})
                    idle = 0.0
            last_state = state
            if state in TERMINAL and not rows:
                quiet += 1
                if quiet >= 2:      # два пустых цикла после финиша — хвост вычитан
                    await ws.send_json({"type": "eof", "state": state})
                    break
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("ws_logs_closed", job_id=str(jid), error=str(e))
    finally:
        bus.unsubscribe(queue, "job_logs")