from __future__ import annotations

import asyncio
import contextlib
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from mf_core.db import get_sessionmaker
from mf_core.models import Job
from mf_core.telemetry import get_logger
from ui.deps import render
from ui.security import read_session_cookie

log = get_logger("mf.ui.ws")
router = APIRouter()


@router.websocket("/ws/jobs")
async def ws_jobs(ws: WebSocket):
    s = ws.app.state.settings
    claims = read_session_cookie(ws.cookies.get(s.ui_session_cookie), s)
    if claims is None:
        await ws.close(code=4401)
        return
    bus = ws.app.state.bus
    if bus.subscribers >= s.ui_ws_max_clients:
        await ws.close(code=4429)
        return
    await ws.accept()
    fmt = ws.query_params.get("format", "html")
    state_filter = ws.query_params.get("state") or ""
    queue = bus.subscribe()
    sm = get_sessionmaker()
    try:
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=25)
            except TimeoutError:
                if fmt == "json":
                    await ws.send_json({"type": "ping"})
                else:
                    await ws.send_text("<!--keepalive-->")
                continue
            # coalescing: если по одной задаче пришли ещё события — берём последнее
            latest = {evt["id"]: evt}
            with contextlib.suppress(asyncio.QueueEmpty):
                for _ in range(50):
                    more = queue.get_nowait()
                    latest[more["id"]] = more
            for job_id, payload in latest.items():
                if state_filter and payload.get("state") != state_filter:
                    continue
                if fmt == "json":
                    await ws.send_json(payload)
                    continue
                async with sm() as sess:
                    job = await sess.get(Job, uuid.UUID(job_id))
                if job is None:
                    continue
                from ui.deps import templates
                html = templates.env.get_template("_job_row.html").render(
                    job=job, oob=True, request=None, user=None
                )
                await ws.send_text(html)
                await ws.send_text(html)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as e:  # noqa: BLE001
        log.debug("ws_closed", error=str(e))
    finally:
        bus.unsubscribe(queue)