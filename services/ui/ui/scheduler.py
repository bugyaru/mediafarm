"""Control plane: leader election + APScheduler + reaper/retention/метрики."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import uuid

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from mf_core import queue as q
from mf_core.config import Settings
from mf_core.db import get_sessionmaker
from mf_core.errors import ConfigInvalidError
from mf_core.media.presets import load_preset, resolve_spec
from mf_core.telemetry import LEASE_REAPED, QUEUE_DEPTH, get_logger

log = get_logger("mf.ui.control")


class ControlPlane:
    def __init__(self, settings: Settings, registry) -> None:
        self.s = settings
        self.registry = registry
        self.leader = False
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._sched: AsyncIOScheduler | None = None
        self._known: dict[str, str] = {}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="control-plane")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        engine = create_async_engine(self.s.db_dsn, poolclass=NullPool)
        try:
            while not self._stopping.is_set():
                try:
                    async with engine.connect() as conn:
                        got = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"),
                                                     {"k": self.s.ui_leader_lock_key}))
                        if not got:
                            self.leader = False
                            await asyncio.sleep(10)
                            continue
                        self.leader = True
                        log.info("control_plane_leader_acquired")
                        try:
                            await self._term(conn)
                        finally:
                            self.leader = False
                            with contextlib.suppress(Exception):
                                await conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                                                   {"k": self.s.ui_leader_lock_key})
                                await conn.commit()
                            log.info("control_plane_leader_released")
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.leader = False
                    log.warning("control_plane_error", error=str(e))
                    await asyncio.sleep(10)
        finally:
            await engine.dispose()

    async def _term(self, conn) -> None:
        sched = AsyncIOScheduler(timezone="UTC")
        self._sched = sched
        sched.add_job(self._reaper, "interval", seconds=self.s.ui_reaper_interval_s,
                      id="reaper", max_instances=1)
        sched.add_job(self._metrics, "interval", seconds=15, id="metrics", max_instances=1)
        sched.add_job(self._sync_schedules, "interval", seconds=self.s.ui_schedule_sync_s,
                      id="sync", max_instances=1, next_run_time=dt.datetime.now(dt.UTC))
        sched.add_job(self._retention, "cron", hour=3, id="retention", max_instances=1)
        sched.start()
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(5)
                await conn.scalar(text("SELECT 1"))     # потеря соединения → выход из термина
        finally:
            sched.shutdown(wait=False)
            self._sched = None
            self._known.clear()

    # ------------------------------ системные ------------------------------
    async def _reaper(self) -> None:
        sm = get_sessionmaker()
        async with sm() as sess, sess.begin():
            reaped = await q.reap(sess, limit=200)
            stale = await q.stale_workers(sess, timeout_s=max(90, self.s.lease_s * 3))
        if reaped:
            LEASE_REAPED.inc(len(reaped))
            log.warning("jobs_reaped", count=len(reaped))
        if stale:
            log.warning("workers_offline", workers=stale)

    async def _metrics(self) -> None:
        sm = get_sessionmaker()
        async with sm() as sess:
            rows = await q.stats(sess)
        for r in rows:
            QUEUE_DEPTH.labels(r["state"], "gpu" if r["requires_gpu"] else "cpu").set(r["n"])

    async def _retention(self) -> None:
        days = self.s.ui_retention_days
        sm = get_sessionmaker()
        async with sm() as sess, sess.begin():
            ev = await sess.execute(text(
                "DELETE FROM job_event WHERE ts < now() - make_interval(days => :d)"), {"d": days})
            jb = await sess.execute(text("""
                DELETE FROM job WHERE state IN ('succeeded','failed','cancelled')
                  AND finished_at < now() - make_interval(days => :d)
            """), {"d": days})
            au = await sess.execute(text(
                "DELETE FROM audit_log WHERE ts < now() - make_interval(days => :d)"),
                {"d": days * 2})
        log.info("retention_done", events=ev.rowcount, jobs=jb.rowcount, audit=au.rowcount)

    # ------------------------------ расписания -----------------------------
    async def _sync_schedules(self) -> None:
        sm = get_sessionmaker()
        async with sm() as sess:
            rows = (await sess.execute(text(
                "SELECT id, name, cron, timezone, enabled FROM schedule WHERE enabled"
            ))).mappings().all()
        sched = self._sched
        if sched is None:
            return
        wanted = {str(r["id"]): r for r in rows}
        for sid in list(self._known):
            if sid not in wanted:
                with contextlib.suppress(Exception):
                    sched.remove_job(sid)
                self._known.pop(sid, None)
        for sid, row in wanted.items():
            signature = f"{row['cron']}|{row['timezone']}"
            if self._known.get(sid) == signature:
                continue
            try:
                trigger = CronTrigger.from_crontab(row["cron"], timezone=row["timezone"])
            except Exception as e:  # noqa: BLE001
                log.warning("bad_cron", schedule=row["name"], error=str(e))
                continue
            sched.add_job(self._emit_schedule, trigger, args=[sid], id=sid,
                          replace_existing=True, coalesce=True, misfire_grace_time=300,
                          max_instances=1)
            self._known[sid] = signature
            job = sched.get_job(sid)
            async with sm() as sess, sess.begin():
                await sess.execute(text("UPDATE schedule SET next_run_at=:n WHERE id=:i"),
                                   {"n": job.next_run_time, "i": uuid.UUID(sid)})

    async def _emit_schedule(self, schedule_id: str) -> None:
        sm = get_sessionmaker()
        sid = uuid.UUID(schedule_id)
        run_slot = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
        async with sm() as sess, sess.begin():
            row = (await sess.execute(text("SELECT * FROM schedule WHERE id=:i AND enabled"),
                                      {"i": sid})).mappings().first()
            if row is None:
                return
            tpl = dict(row["job_template"] or {})
            try:
                preset = await load_preset(sess, preset_id=tpl.get("preset_id"),
                                           name=tpl.get("preset_name"))
            except ConfigInvalidError as e:
                log.error("schedule_preset_missing", schedule=row["name"], error=str(e))
                return
            spec = resolve_spec(preset, tpl.get("overrides"))
            targets = await self._targets(sess, tpl)
            created = 0
            for target in targets:
                params = {**(tpl.get("params") or {}), **target["params"]}
                key = hashlib.sha256(
                    f"{sid}|{run_slot.isoformat()}|{target['key']}|{preset.version}".encode()
                ).hexdigest()
                _, ok = await q.enqueue(
                    sess, kind=tpl.get("kind", "transcode"), preset_id=preset.id,
                    preset_version=preset.version, spec=spec.to_db(), params=params,
                    asset_id=target.get("asset_id"), schedule_id=sid,
                    priority=int(tpl.get("priority", 100)), selector=spec.selector,
                    requires_gpu=spec.requires_gpu, gpu_sessions=spec.gpu_sessions,
                    cpu_cost=spec.cpu_cost, max_attempts=spec.max_attempts,
                    idempotency_key=key, created_by=f"schedule:{row['name']}",
                )
                created += int(ok)
            await sess.execute(text("UPDATE schedule SET last_run_at=now() WHERE id=:i"),
                               {"i": sid})
        log.info("schedule_fired", schedule_id=schedule_id, created=created,
                 targets=len(targets))

    async def _targets(self, sess, tpl: dict) -> list[dict]:
        if tpl.get("url"):
            return [{"key": tpl["url"], "params": {"url": tpl["url"]}}]
        if tpl.get("input_uri"):
            return [{"key": tpl["input_uri"], "params": {"input_uri": tpl["input_uri"]}}]
        if tpl.get("asset_id"):
            return [{"key": tpl["asset_id"], "params": {}, "asset_id": uuid.UUID(tpl["asset_id"])}]
        query = tpl.get("asset_query") or {}
        if query:
            sql = "SELECT id, uri, kind FROM asset WHERE state = :state"
            args: dict = {"state": query.get("state", "new"),
                          "limit": int(query.get("limit", 50))}
            if query.get("watch_folder_id"):
                sql += " AND watch_folder_id = :wf"
                args["wf"] = uuid.UUID(query["watch_folder_id"])
            sql += " ORDER BY discovered_at LIMIT :limit"
            rows = (await sess.execute(text(sql), args)).mappings().all()
            return [{"key": str(r["id"]), "asset_id": r["id"],
                     "params": {"url" if r["kind"] != "file" else "input_uri": r["uri"]}}
                    for r in rows]
        return []