from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from mf_core import queue as q
from mf_core.errors import LeaseLost

pytestmark = pytest.mark.integration

CPU = {"arch": "x86_64"}
GPU = {"arch": "x86_64", "gpu": "nvidia", "nvenc": "h264_nvenc,hevc_nvenc,av1_nvenc"}


def _sm(dsn, **kw):
    return async_sessionmaker(create_async_engine(dsn, **kw), expire_on_commit=False)


async def test_parallel_claims_no_duplicates(migrated_db):
    engine = create_async_engine(migrated_db, pool_size=20, max_overflow=40)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    total = 200
    async with sm() as s, s.begin():
        for i in range(total):
            await q.enqueue(s, params={"n": i}, idempotency_key=f"par-{uuid.uuid4()}-{i}")

    async def worker(wid: str) -> list[uuid.UUID]:
        got = []
        while True:
            async with sm() as s, s.begin():
                job = await q.claim(s, worker_id=wid, labels=CPU, cpu_free=4, gpu_free=0)
            if job is None:
                return got
            got.append(job.id)

    results = await asyncio.gather(*[worker(f"w{i}") for i in range(40)])
    flat = [j for sub in results for j in sub]
    assert len(flat) == total
    assert len(set(flat)) == total, "одна задача выдана двум воркерам"
    await engine.dispose()


async def test_selector_and_gpu_routing(migrated_db):
    sm = _sm(migrated_db, poolclass=NullPool)
    async with sm() as s, s.begin():
        jid, _ = await q.enqueue(s, requires_gpu=True, gpu_sessions=1,
                                 selector={"gpu": "nvidia"}, params={"gpu": True})
    async with sm() as s, s.begin():
        assert await q.claim(s, worker_id="cpu1", labels=CPU, cpu_free=8, gpu_free=0) is None
    async with sm() as s, s.begin():
        assert await q.claim(s, worker_id="gpu0", labels=GPU, cpu_free=8, gpu_free=0) is None
    async with sm() as s, s.begin():
        job = await q.claim(s, worker_id="gpu1", labels=GPU, cpu_free=8, gpu_free=6)
        assert job and job.id == jid and job.attempts == 1


async def test_lease_heartbeat_and_reaper(migrated_db):
    sm = _sm(migrated_db, poolclass=NullPool)
    async with sm() as s, s.begin():
        jid, _ = await q.enqueue(s, max_attempts=3)
    async with sm() as s, s.begin():
        job = await q.claim(s, worker_id="w", labels=CPU, cpu_free=4, gpu_free=0, lease_s=60)
        assert job.id == jid
    async with sm() as s, s.begin():
        assert await q.heartbeat(s, job_id=jid, worker_id="w", progress={"pct": 10}) is False
    # «под умер» — lease в прошлом
    async with sm() as s, s.begin():
        await s.execute(text("UPDATE job SET lease_expires_at = now() - interval '1 min' WHERE id=:i"),
                        {"i": jid})
    async with sm() as s, s.begin():
        reaped = await q.reap(s)
        assert (jid, "queued") in reaped
    async with sm() as s, s.begin():
        with pytest.raises(LeaseLost):
            await q.heartbeat(s, job_id=jid, worker_id="w")


async def test_retry_until_failed(migrated_db):
    sm = _sm(migrated_db, poolclass=NullPool)
    async with sm() as s, s.begin():
        jid, _ = await q.enqueue(s, max_attempts=2)
    for expected in ("queued", "failed"):
        async with sm() as s, s.begin():
            await q.claim(s, worker_id="w", labels=CPU, cpu_free=4, gpu_free=0)
        async with sm() as s, s.begin():
            state = await q.retry(s, job_id=jid, worker_id="w",
                                  error={"class": "transient_network"}, backoff_s=0)
            assert state == expected
    async with sm() as s, s.begin():
        assert await q.requeue_manual(s, jid) is True


async def test_concurrency_limit(migrated_db):
    sm = _sm(migrated_db, poolclass=NullPool)
    key = f"ftp-lim-{uuid.uuid4().hex[:6]}"
    async with sm() as s, s.begin():
        for _ in range(3):
            await q.enqueue(s, concurrency_key=key, concurrency_limit=1)
    async with sm() as s, s.begin():
        first = await q.claim(s, worker_id="w", labels=CPU, cpu_free=8, gpu_free=0)
        assert first is not None
    async with sm() as s, s.begin():
        assert await q.claim(s, worker_id="w", labels=CPU, cpu_free=8, gpu_free=0) is None


async def test_idempotent_enqueue_and_cancel(migrated_db):
    sm = _sm(migrated_db, poolclass=NullPool)
    ik = uuid.uuid4().hex
    async with sm() as s, s.begin():
        a, created_a = await q.enqueue(s, idempotency_key=ik)
    async with sm() as s, s.begin():
        b, created_b = await q.enqueue(s, idempotency_key=ik)
    assert a == b and created_a and not created_b
    async with sm() as s, s.begin():
        assert await q.cancel(s, a) == "cancelled"
    async with sm() as s, s.begin():
        await q.step_start(s, a, 0, "acquire", "python", "echo hi")
        await q.step_finish(s, a, 0, "skipped", metrics={"x": 1})
        await q.add_event(s, a, "test", "hello")
        assert isinstance(await q.stats(s), list)


async def test_advisory_lock_exclusive(migrated_db):
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    key = 999_001
    async with q.advisory_lock(engine, key) as first:
        assert first
        async with q.advisory_lock(engine, key) as second:
            assert not second
    async with q.advisory_lock(engine, key) as third:
        assert third
    await engine.dispose()