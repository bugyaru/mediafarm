from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mf_core import db, queue as q
from mf_core.config import Settings, reset_settings_cache
from worker.main import Worker
from worker.pipeline import Pipeline

pytestmark = pytest.mark.integration


def _settings(dsn, tmp_path, **kw):
    base = dict(role="worker", db_dsn=dsn, worker_id=f"test-{uuid.uuid4().hex[:6]}",
                cpu_slots=2, gpu_slots=0, scratch=str(tmp_path), metrics_port=0,
                idle_poll_s=0.05, heartbeat_s=1, progress_throttle_s=0.2, lease_s=10,
                on_sigterm="requeue", drain_timeout_s=10)
    base.update(kw)
    return Settings(**base)


async def _wait(sm, job_id, states, timeout=25):
    async def poll():
        while True:
            async with sm() as s:
                st = await s.scalar(text("SELECT state FROM job WHERE id=:i"), {"i": job_id})
            if st in states:
                return st
            await asyncio.sleep(0.1)
    return await asyncio.wait_for(poll(), timeout)


async def test_worker_completes_jobs(migrated_db, tmp_path, monkeypatch):
    sm = async_sessionmaker(create_async_engine(migrated_db, poolclass=NullPool),
                            expire_on_commit=False)

    async def fake_run(self):
        await asyncio.sleep(0.2)
        return {"artifacts": [], "metrics": {"engine": "python"}}

    monkeypatch.setattr(Pipeline, "run", fake_run)
    ids = []
    async with sm() as s, s.begin():
        for _ in range(5):
            jid, _ = await q.enqueue(s, spec={"name": "t", "steps": [{"id": "acquire"}]},
                                     params={"input_uri": "x"})
            ids.append(jid)

    w = Worker(_settings(migrated_db, tmp_path))
    task = asyncio.create_task(w.run())
    try:
        for jid in ids:
            assert await _wait(sm, jid, {"succeeded"}) == "succeeded"
    finally:
        w.request_stop()
        await asyncio.wait_for(task, 20)
        await db.dispose()
    async with sm() as s:
        state = await s.scalar(text("SELECT state FROM worker WHERE id=:i"),
                               {"i": w.s.worker_id})
    assert state == "offline"


async def test_drain_requeues_running_job(migrated_db, tmp_path, monkeypatch):
    sm = async_sessionmaker(create_async_engine(migrated_db, poolclass=NullPool),
                            expire_on_commit=False)
    started = asyncio.Event()

    async def slow_run(self):
        started.set()
        while not self.ctx.cancel.is_set():
            await asyncio.sleep(0.05)
        from mf_core.errors import CancelledByUser
        raise CancelledByUser("drain")

    monkeypatch.setattr(Pipeline, "run", slow_run)
    async with sm() as s, s.begin():
        jid, _ = await q.enqueue(s, spec={"name": "t", "steps": [{"id": "acquire"}]})

    w = Worker(_settings(migrated_db, tmp_path, on_sigterm="requeue"))
    task = asyncio.create_task(w.run())
    try:
        await asyncio.wait_for(started.wait(), 20)
        w.request_stop()
        await asyncio.wait_for(task, 25)
    finally:
        await db.dispose()
    async with sm() as s:
        row = (await s.execute(text("SELECT state, attempts FROM job WHERE id=:i"),
                               {"i": jid})).first()
    assert row[0] == "queued" and row[1] == 0     # drain не считается попыткой


async def test_failure_marks_failed_after_retries(migrated_db, tmp_path, monkeypatch):
    sm = async_sessionmaker(create_async_engine(migrated_db, poolclass=NullPool),
                            expire_on_commit=False)

    async def boom(self):
        from mf_core.errors import SourceInvalidError
        raise SourceInvalidError("битый источник")

    monkeypatch.setattr(Pipeline, "run", boom)
    async with sm() as s, s.begin():
        jid, _ = await q.enqueue(s, spec={"name": "t", "steps": [{"id": "acquire"}]},
                                 max_attempts=1)
    w = Worker(_settings(migrated_db, tmp_path))
    task = asyncio.create_task(w.run())
    try:
        assert await _wait(sm, jid, {"failed"}) == "failed"
    finally:
        w.request_stop()
        await asyncio.wait_for(task, 20)
        await db.dispose()