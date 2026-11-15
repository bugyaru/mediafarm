"""Очередь задач на PostgreSQL: SKIP LOCKED + lease + reaper."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from mf_core.errors import LeaseLost

CLAIM = text("""
WITH candidate AS (
    SELECT j.id
    FROM job j
    WHERE j.state = 'queued'
      AND j.run_after <= now()
      AND CAST(:labels AS jsonb) @> j.selector
      AND (NOT j.requires_gpu OR :gpu_free > 0)
      AND j.gpu_sessions <= :gpu_free
      AND j.cpu_cost <= :cpu_free
      AND (j.concurrency_key IS NULL OR j.concurrency_limit = 0 OR (
              SELECT count(*) FROM job r
              WHERE r.state = 'running' AND r.concurrency_key = j.concurrency_key
          ) < j.concurrency_limit)
    ORDER BY j.priority DESC, j.run_after ASC, j.id ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE job j
SET state = 'running',
    worker_id = :worker_id,
    lease_expires_at = now() + make_interval(secs => :lease_s),
    attempts = j.attempts + 1,
    started_at = COALESCE(j.started_at, now()),
    cancel_requested = false,
    error = '{}'::jsonb,
    updated_at = now()
FROM candidate c
WHERE j.id = c.id
RETURNING j.*;
""")

HEARTBEAT = text("""
UPDATE job
SET lease_expires_at = now() + make_interval(secs => :lease_s),
    progress = COALESCE(CAST(:progress AS jsonb), progress),
    updated_at = now()
WHERE id = :job_id AND worker_id = :worker_id AND state = 'running'
RETURNING cancel_requested;
""")

COMPLETE = text("""
UPDATE job
SET state = :state, result = CAST(:result AS jsonb), error = CAST(:error AS jsonb),
    progress = CAST(:progress AS jsonb), finished_at = now(),
    lease_expires_at = NULL, updated_at = now()
WHERE id = :job_id AND worker_id = :worker_id AND state = 'running'
RETURNING id;
""")

RETRY = text("""
UPDATE job
SET state = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
    worker_id = NULL, lease_expires_at = NULL,
    run_after = now() + make_interval(secs => :backoff_s),
    finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
    error = CAST(:error AS jsonb), updated_at = now()
WHERE id = :job_id AND worker_id = :worker_id AND state = 'running'
RETURNING state, attempts, max_attempts;
""")

REAP = text("""
WITH stale AS (
    SELECT id, attempts, max_attempts FROM job
    WHERE state = 'running' AND lease_expires_at < now()
    FOR UPDATE SKIP LOCKED
    LIMIT :limit
)
UPDATE job j SET
    state = CASE WHEN s.attempts < s.max_attempts THEN 'queued' ELSE 'failed' END,
    worker_id = NULL, lease_expires_at = NULL,
    run_after = now() + make_interval(secs => LEAST(300, 15 * s.attempts)),
    finished_at = CASE WHEN s.attempts < s.max_attempts THEN NULL ELSE now() END,
    error = jsonb_build_object('class','infra','code','lease_expired','retryable',true),
    updated_at = now()
FROM stale s WHERE j.id = s.id
RETURNING j.id, j.state;
""")

ENQUEUE = text("""
INSERT INTO job (kind, state, priority, preset_id, preset_version, spec, params, selector,
                 requires_gpu, gpu_sessions, cpu_cost, concurrency_key, concurrency_limit,
                 asset_id, watch_folder_id, schedule_id, idempotency_key, max_attempts,
                 run_after, created_by)
VALUES (:kind, 'queued', :priority, :preset_id, :preset_version,
        CAST(:spec AS jsonb), CAST(:params AS jsonb), CAST(:selector AS jsonb),
        :requires_gpu, :gpu_sessions, :cpu_cost, :concurrency_key, :concurrency_limit,
        :asset_id, :watch_folder_id, :schedule_id, :idempotency_key, :max_attempts,
        COALESCE(CAST(:run_after AS timestamptz), now()), :created_by)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id;
""")


@dataclass(slots=True)
class ClaimedJob:
    id: uuid.UUID
    kind: str
    attempts: int
    max_attempts: int
    preset_id: uuid.UUID | None
    preset_version: int | None
    spec: dict
    params: dict
    asset_id: uuid.UUID | None
    watch_folder_id: uuid.UUID | None
    requires_gpu: bool
    gpu_sessions: int
    cpu_cost: int
    concurrency_key: str | None
    lease_expires_at: dt.datetime
    priority: int

    @classmethod
    def from_row(cls, r: Any) -> ClaimedJob:
        return cls(
            id=r["id"], kind=r["kind"], attempts=r["attempts"], max_attempts=r["max_attempts"],
            preset_id=r["preset_id"], preset_version=r["preset_version"],
            spec=dict(r["spec"] or {}), params=dict(r["params"] or {}),
            asset_id=r["asset_id"], watch_folder_id=r["watch_folder_id"],
            requires_gpu=r["requires_gpu"], gpu_sessions=r["gpu_sessions"],
            cpu_cost=r["cpu_cost"], concurrency_key=r["concurrency_key"],
            lease_expires_at=r["lease_expires_at"], priority=r["priority"],
        )


def _j(v: Any) -> str:
    return json.dumps(v if v is not None else {}, ensure_ascii=False)


async def claim(
        session: AsyncSession, *, worker_id: str, labels: dict, cpu_free: int,
        gpu_free: int, lease_s: int = 60,
) -> ClaimedJob | None:
    row = (await session.execute(CLAIM, {
        "labels": _j(labels), "gpu_free": gpu_free, "cpu_free": cpu_free,
        "worker_id": worker_id, "lease_s": lease_s,
    })).mappings().first()
    return ClaimedJob.from_row(row) if row else None


async def heartbeat(
        session: AsyncSession, *, job_id: uuid.UUID, worker_id: str,
        progress: dict | None = None, lease_s: int = 60,
) -> bool:
    """Продлевает lease. Возвращает cancel_requested. LeaseLost — если задача уже не наша."""
    row = (await session.execute(HEARTBEAT, {
        "job_id": job_id, "worker_id": worker_id,
        "progress": _j(progress) if progress is not None else None, "lease_s": lease_s,
    })).first()
    if row is None:
        raise LeaseLost(f"lease потерян для job {job_id}")
    return bool(row[0])


async def complete(
        session: AsyncSession, *, job_id: uuid.UUID, worker_id: str, state: str,
        result: dict | None = None, error: dict | None = None, progress: dict | None = None,
) -> bool:
    row = (await session.execute(COMPLETE, {
        "job_id": job_id, "worker_id": worker_id, "state": state,
        "result": _j(result), "error": _j(error),
        "progress": _j(progress or {"pct": 100 if state == "succeeded" else None}),
    })).first()
    return row is not None


async def retry(
        session: AsyncSession, *, job_id: uuid.UUID, worker_id: str,
        error: dict, backoff_s: float,
) -> str | None:
    row = (await session.execute(RETRY, {
        "job_id": job_id, "worker_id": worker_id,
        "error": _j(error), "backoff_s": backoff_s,
    })).first()
    return row[0] if row else None


async def reap(session: AsyncSession, limit: int = 200) -> list[tuple[uuid.UUID, str]]:
    rows = (await session.execute(REAP, {"limit": limit})).all()
    return [(r[0], r[1]) for r in rows]


async def enqueue(
        session: AsyncSession, *, kind: str = "transcode",
        preset_id: uuid.UUID | None = None, preset_version: int | None = None,
        spec: dict | None = None, params: dict | None = None,
        asset_id: uuid.UUID | None = None, watch_folder_id: uuid.UUID | None = None,
        schedule_id: uuid.UUID | None = None, priority: int = 100,
        selector: dict | None = None, requires_gpu: bool = False, gpu_sessions: int = 0,
        cpu_cost: int = 1, concurrency_key: str | None = None, concurrency_limit: int = 0,
        max_attempts: int = 3, run_after: dt.datetime | None = None,
        idempotency_key: str | None = None, created_by: str | None = None,
) -> tuple[uuid.UUID, bool]:
    args = {
        "kind": kind, "priority": priority, "preset_id": preset_id,
        "preset_version": preset_version, "spec": _j(spec), "params": _j(params),
        "selector": _j(selector), "requires_gpu": requires_gpu, "gpu_sessions": gpu_sessions,
        "cpu_cost": cpu_cost, "concurrency_key": concurrency_key,
        "concurrency_limit": concurrency_limit, "asset_id": asset_id,
        "watch_folder_id": watch_folder_id, "schedule_id": schedule_id,
        "idempotency_key": idempotency_key, "max_attempts": max_attempts,
        "run_after": run_after, "created_by": created_by,
    }
    row = (await session.execute(ENQUEUE, args)).first()
    if row:
        return row[0], True
    existing = (await session.execute(
        text("SELECT id FROM job WHERE idempotency_key = :k"), {"k": idempotency_key}
    )).first()
    if existing is None:
        raise RuntimeError("enqueue: конфликт без существующей записи")
    return existing[0], False


async def cancel(session: AsyncSession, job_id: uuid.UUID) -> str | None:
    row = (await session.execute(text("""
        UPDATE job SET
            cancel_requested = true,
            state = CASE WHEN state = 'queued' THEN 'cancelled' ELSE state END,
            finished_at = CASE WHEN state = 'queued' THEN now() ELSE finished_at END,
            updated_at = now()
        WHERE id = :id AND state IN ('queued','running')
        RETURNING state;
    """), {"id": job_id})).first()
    return row[0] if row else None


async def requeue_manual(session: AsyncSession, job_id: uuid.UUID) -> bool:
    res = await session.execute(text("""
        UPDATE job SET state='queued', attempts=0, worker_id=NULL, lease_expires_at=NULL,
                       error='{}'::jsonb, progress='{}'::jsonb, finished_at=NULL,
                       cancel_requested=false, run_after=now(), updated_at=now()
        WHERE id=:id AND state IN ('failed','cancelled')
    """), {"id": job_id})
    return (res.rowcount or 0) > 0


# ------------------------------- workers -----------------------------------
REGISTER_WORKER = text("""
INSERT INTO worker (id, kind, version, host, labels, capabilities, cpu_slots, gpu_slots, state)
VALUES (:id, :kind, :version, :host, CAST(:labels AS jsonb), CAST(:caps AS jsonb),
        :cpu_slots, :gpu_slots, :state)
ON CONFLICT (id) DO UPDATE SET
    kind = EXCLUDED.kind, version = EXCLUDED.version, host = EXCLUDED.host,
    labels = EXCLUDED.labels, capabilities = EXCLUDED.capabilities,
    cpu_slots = EXCLUDED.cpu_slots, gpu_slots = EXCLUDED.gpu_slots,
    cpu_used = 0, gpu_used = 0, state = EXCLUDED.state,
    started_at = now(), heartbeat_at = now(), updated_at = now();
""")


async def register_worker(session: AsyncSession, **kw) -> None:
    await session.execute(REGISTER_WORKER, {
        "id": kw["id"], "kind": kw["kind"], "version": kw["version"],
        "host": kw.get("host"), "labels": _j(kw.get("labels")),
        "caps": _j(kw.get("capabilities")), "cpu_slots": kw.get("cpu_slots", 0),
        "gpu_slots": kw.get("gpu_slots", 0), "state": kw.get("state", "online"),
    })


async def worker_heartbeat(
        session: AsyncSession, worker_id: str, *, cpu_used: int, gpu_used: int,
        state: str = "online",
) -> None:
    await session.execute(text("""
        UPDATE worker SET heartbeat_at = now(), cpu_used = :cu, gpu_used = :gu,
                          state = :state, updated_at = now()
        WHERE id = :id
    """), {"id": worker_id, "cu": cpu_used, "gu": gpu_used, "state": state})


async def stale_workers(session: AsyncSession, timeout_s: int = 90) -> list[str]:
    rows = (await session.execute(text("""
        UPDATE worker SET state='offline', updated_at=now()
        WHERE state <> 'offline' AND heartbeat_at < now() - make_interval(secs => :t)
        RETURNING id
    """), {"t": timeout_s})).all()
    return [r[0] for r in rows]


# ------------------------------ шаги/события --------------------------------
async def step_start(
        session: AsyncSession, job_id: uuid.UUID, idx: int, name: str,
        engine: str | None = None, cmdline: str | None = None,
) -> None:
    await session.execute(text("""
        INSERT INTO job_step (job_id, idx, name, state, engine, cmdline, started_at)
        VALUES (:j, :i, :n, 'running', :e, :c, now())
        ON CONFLICT (job_id, idx) DO UPDATE SET
            state='running', engine=EXCLUDED.engine, cmdline=EXCLUDED.cmdline,
            started_at=now(), finished_at=NULL, error='{}'::jsonb
    """), {"j": job_id, "i": idx, "n": name, "e": engine, "c": cmdline})


async def step_finish(
        session: AsyncSession, job_id: uuid.UUID, idx: int, state: str,
        metrics: dict | None = None, error: dict | None = None, log_uri: str | None = None,
) -> None:
    await session.execute(text("""
        UPDATE job_step SET state=:s, metrics=CAST(:m AS jsonb), error=CAST(:e AS jsonb),
                            log_uri=COALESCE(:l, log_uri), finished_at=now()
        WHERE job_id=:j AND idx=:i
    """), {"j": job_id, "i": idx, "s": state, "m": _j(metrics), "e": _j(error), "l": log_uri})


async def add_event(
        session: AsyncSession, job_id: uuid.UUID, type: str, message: str = "",
        level: str = "info", data: dict | None = None,
) -> None:
    await session.execute(text("""
        INSERT INTO job_event (job_id, level, type, message, data)
        VALUES (:j, :lv, :t, :m, CAST(:d AS jsonb))
    """), {"j": job_id, "lv": level, "t": type, "m": message[:4000], "d": _j(data)})


async def stats(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(text("""
        SELECT state, requires_gpu, count(*) AS n,
               COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at))), 0)::int AS oldest_s
        FROM job GROUP BY 1, 2 ORDER BY 1, 2
    """))).mappings().all()
    return [dict(r) for r in rows]


async def backlog(session: AsyncSession, watch_folder_id: uuid.UUID) -> int:
    return int(await session.scalar(text("""
        SELECT count(*) FROM job WHERE watch_folder_id = :w AND state IN ('queued','running')
    """), {"w": watch_folder_id}) or 0)


# --------------------------- advisory locks ---------------------------------
@asynccontextmanager
async def advisory_lock(engine: AsyncEngine, key: int) -> AsyncIterator[bool]:
    """Session-level lock на выделенном соединении (шардирование папок, leader election)."""
    async with engine.connect() as conn:
        got = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}))
        try:
            yield got
        finally:
            if got:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await conn.commit()