from __future__ import annotations

import asyncio
import contextlib
import signal
import time
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from mf_core import db, queue as q
from mf_core.config import Settings, get_settings
from mf_core.errors import CancelledByUser, LeaseLost, classify, exp_backoff
from mf_core.media.hwaccel import gpu_snapshot
from mf_core.media.presets import load_preset, resolve_spec, snapshot, spec_from_job
from mf_core.storage import StorageRegistry
from mf_core.telemetry import (
    JOB_DURATION, JOBS_TOTAL, WORKER_SLOTS, HealthState, bind_context, clear_context,
    get_logger, serve_observability, setup_logging,
)
from worker import scratch
from worker.caps import detect
from worker.context import JobContext
from worker.drain import is_draining
from worker.logsink import LogSink
from worker.pipeline import Pipeline
from worker.slots import Slots

log = get_logger("mf.worker")

DRAIN_REQUEUE = text("""
UPDATE job SET state='queued', worker_id=NULL, lease_expires_at=NULL,
               attempts = GREATEST(0, attempts - 1), run_after = now(),
               progress='{}'::jsonb, cancel_requested=false, updated_at=now()
WHERE id=:id AND worker_id=:w AND state='running'
""")

class Worker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.stopping = asyncio.Event()
        self.state = HealthState(healthy=True, ready=False, detail={"worker_id": self.s.worker_id})
        self.registry = StorageRegistry()
        self.hw = None
        self.slots: Slots | None = None
        self.tasks: set[asyncio.Task] = set()
        self.contexts: dict[uuid.UUID, JobContext] = {}
        self.sf = None
        self._server = None

    # ------------------------------ lifecycle ------------------------------
    def request_stop(self, reason: str = "sigterm") -> None:
        if self.stopping.is_set():
            return
        log.info("stop_requested", reason=reason, on_sigterm=self.s.on_sigterm)
        self.stopping.set()
        self.state.ready = False
        if self.s.on_sigterm == "requeue":
            for ctx in self.contexts.values():
                ctx.draining = True
                ctx.cancel_reason = "drain"
                ctx.cancel.set()

    async def run(self) -> None:
        setup_logging()
        s = self.s
        Path(s.scratch).mkdir(parents=True, exist_ok=True)
        self._server = await serve_observability(s.metrics_port, self.state)
        self.hw, labels, caps = await detect(s)
        gpu_slots = s.effective_gpu_slots if self.hw.cuda else 0
        self.slots = Slots(cpu_total=max(1, s.cpu_slots), gpu_total=gpu_slots)
        self.sf = async_sessionmaker(db.get_engine(), expire_on_commit=False, autoflush=False)

        async with self.sf() as sess, sess.begin():
            await self.registry.refresh(sess)
            await q.register_worker(
                sess, id=s.worker_id, kind=s.kind, version=__import__("worker").__version__,
                host=s.worker_id, labels=labels, capabilities=caps,
                cpu_slots=self.slots.cpu_total, gpu_slots=self.slots.gpu_total, state="online",
            )
        scratch.gc(s, keep=set())
        self._install_signals()
        self.state.ready = True
        self.state.detail.update({"cuda": self.hw.cuda, "gpu_slots": gpu_slots,
                                  "cpu_slots": self.slots.cpu_total})
        log.info("worker_started", worker_id=s.worker_id, kind=s.kind, **self.slots.snapshot())

        hb = asyncio.create_task(self._worker_heartbeat(), name="worker-hb")
        try:
            await self._claim_loop()
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
            await self._drain()
            await self._shutdown()

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.request_stop, sig.name)

    async def _shutdown(self) -> None:
        with contextlib.suppress(Exception):
            async with self.sf() as sess, sess.begin():
                await q.worker_heartbeat(sess, self.s.worker_id, cpu_used=0, gpu_used=0,
                                         state="offline")
        await self.registry.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        await db.dispose()
        log.info("worker_stopped", worker_id=self.s.worker_id)

    # ------------------------------- heartbeat -----------------------------
    async def _worker_heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.s.heartbeat_s)
            try:
                async with self.sf() as sess, sess.begin():
                    await q.worker_heartbeat(
                        sess, self.s.worker_id, cpu_used=self.slots.cpu_used,
                        gpu_used=self.slots.gpu_used,
                        state="draining" if self.stopping.is_set() else "online",
                    )
                    st = await sess.scalar(text("SELECT state FROM worker WHERE id=:i"),
                                           {"i": self.s.worker_id})
                    if st == "draining":
                        self.request_stop("ui-drain")
                    await self.registry.ensure_fresh(sess)
                if self.hw is not None:
                    await gpu_snapshot(self.hw)
                for kind in ("cpu", "gpu"):
                    WORKER_SLOTS.labels(kind, "used").set(getattr(self.slots, f"{kind}_used"))
                    WORKER_SLOTS.labels(kind, "total").set(getattr(self.slots, f"{kind}_total"))
                scratch.gc(self.s, keep={str(j) for j in self.contexts})
            except Exception as e:  # noqa: BLE001
                log.warning("worker_heartbeat_failed", error=str(e))

    async def _job_heartbeat(self, ctx: JobContext) -> None:
        interval = max(1.0, min(self.s.heartbeat_s, self.s.progress_throttle_s))
        last_ok = time.monotonic()
        while True:
            await asyncio.sleep(interval)
            try:
                await ctx.heartbeat()
                last_ok = time.monotonic()
            except LeaseLost:
                ctx.lose_lease()
                return
            except Exception as e:  # noqa: BLE001
                log.warning("job_heartbeat_failed", job_id=str(ctx.job.id), error=str(e))
                if time.monotonic() - last_ok > self.s.lease_s / 2:
                    log.error("job_heartbeat_giving_up", job_id=str(ctx.job.id))
                    ctx.lose_lease()
                    return

    # ------------------------------ claim loop -----------------------------
    async def _claim_loop(self) -> None:
        s = self.s
        while not self.stopping.is_set():
            if is_draining(s):
                self.request_stop("preStop-sentinel")
                break
            if not self.slots.has_free():
                await asyncio.sleep(0.2)
                continue
            gpu_free = self.slots.gpu_free
            if self.hw is not None and self.hw.cuda and self.hw.gpus:
                if max(g.free_mem_mb for g in self.hw.gpus) < s.gpu_min_free_mem_mb:
                    gpu_free = 0
            try:
                async with self.sf() as sess, sess.begin():
                    job = await q.claim(
                        sess, worker_id=s.worker_id, labels=self.hw.labels() if self.hw else {},
                        cpu_free=self.slots.cpu_free, gpu_free=gpu_free, lease_s=s.lease_s,
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("claim_failed", error=str(e))
                await asyncio.sleep(s.idle_poll_s * 2)
                continue
            if job is None:
                await asyncio.sleep(s.idle_poll_s)
                continue
            if not self.slots.can_fit(job.cpu_cost, job.gpu_sessions, job.requires_gpu):
                # не наш профиль — вернём в очередь без штрафа
                async with self.sf() as sess, sess.begin():
                    await sess.execute(DRAIN_REQUEUE, {"id": job.id, "w": s.worker_id})
                await asyncio.sleep(s.idle_poll_s)
                continue
            self.slots.reserve(job)
            task = asyncio.create_task(self._execute(job), name=f"job-{job.id}")
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    # ------------------------------- execute -------------------------------
    async def _execute(self, job: q.ClaimedJob) -> None:
        s = self.s
        bind_context(job_id=job.id, worker_id=s.worker_id, attempt=job.attempts)
        started = time.monotonic()
        workdir = scratch.job_dir(s, job.id)
        ctx: JobContext | None = None
        hb: asyncio.Task | None = None
        try:
            async with self.sf() as sess, sess.begin():
                if job.spec and job.spec.get("steps"):
                    spec = spec_from_job(job.spec)
                else:
                    preset = await load_preset(sess, preset_id=job.preset_id)
                    spec = resolve_spec(preset, job.params.get("overrides"))
                    await sess.execute(
                        text("UPDATE job SET spec=CAST(:s AS jsonb), preset_version=:v WHERE id=:i"),
                        {"s": __import__("json").dumps(snapshot(spec)), "v": preset.version,
                         "i": job.id},
                    )
                asset = None
                if job.asset_id:
                    row = (await sess.execute(text(
                        "SELECT id, uri, kind, meta, storage_id FROM asset WHERE id=:i"),
                        {"i": job.asset_id})).mappings().first()
                    asset = dict(row) if row else None

            ctx = JobContext(job=job, spec=spec, settings=s, registry=self.registry,
                             hw=self.hw, session_factory=self.sf, workdir=workdir)
            ctx.asset = asset
            ctx.logs = LogSink(job_id=job.id, attempt=job.attempts, session_factory=self.sf,
                               settings=s, workdir=workdir)
            await ctx.logs.start()
            ctx.log(f"задача {job.id} · попытка {job.attempts}/{job.max_attempts} · "
                    f"воркер {s.worker_id} ({s.kind}) · пресет {spec.name}")
            self.contexts[job.id] = ctx
            hb = asyncio.create_task(self._job_heartbeat(ctx), name=f"hb-{job.id}")

            result = await Pipeline(ctx).run()
            await ctx.flush_logs()
            log_uri, logs = await self._upload_logs(ctx)
            if logs:
                result["logs"] = logs
            async with self.sf() as sess, sess.begin():
                await q.complete(sess, job_id=job.id, worker_id=s.worker_id,
                                 state="succeeded", result=result,
                                 progress={"pct": 100.0, "step": "done"})
            JOBS_TOTAL.labels("succeeded", "").inc()
            JOB_DURATION.labels(job.kind, ctx.metrics.get("engine", "python"),
                                "succeeded").observe(time.monotonic() - started)
            scratch.cleanup(workdir)
            log.info("job_succeeded", job_id=str(job.id),
                     elapsed_s=round(time.monotonic() - started, 2), log_uri=log_uri)

        except CancelledByUser:
            await self._on_cancel(job, ctx)
        except LeaseLost:
            JOBS_TOTAL.labels("requeued", "lease_lost").inc()
            log.warning("job_lease_lost", job_id=str(job.id))
        except Exception as e:  # noqa: BLE001
            await self._on_error(job, ctx, e)
        finally:
            if hb is not None:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb
            if ctx is not None and ctx.logs is not None:
                await ctx.logs.close()
            self.contexts.pop(job.id, None)
            self.slots.release(job)
            clear_context()

    async def _on_cancel(self, job: q.ClaimedJob, ctx: JobContext | None) -> None:
        reason = ctx.cancel_reason if ctx else None
        if ctx is not None:
            ctx.log(f"задача остановлена: {reason or 'user'}", "warning")
        if reason == "drain":
            async with self.sf() as sess, sess.begin():
                await sess.execute(DRAIN_REQUEUE, {"id": job.id, "w": self.s.worker_id})
            JOBS_TOTAL.labels("requeued", "drain").inc()
            log.info("job_requeued_on_drain", job_id=str(job.id))
            return
        await self._upload_logs(ctx)
        async with self.sf() as sess, sess.begin():
            await q.complete(sess, job_id=job.id, worker_id=self.s.worker_id,
                             state="cancelled", error={"class": "cancelled", "code": reason})
        JOBS_TOTAL.labels("cancelled", reason or "user").inc()
        scratch.cleanup(scratch.job_dir(self.s, job.id))

    async def _on_error(self, job: q.ClaimedJob, ctx: JobContext | None, exc: Exception) -> None:
        err = classify(exc)
        tail = list(getattr(err, "tail", None) or err.as_dict().get("tail") or [])
        log.error("job_failed", job_id=str(job.id), error_class=err.error_class,
                  message=err.message, attempt=job.attempts, tail_lines=len(tail))
        if ctx is not None:
            ctx.log(f"задача упала: {err.error_class}: {err.message}", "error")
        log_uri, logs = await self._upload_logs(ctx)

        if err.retryable and job.attempts < job.max_attempts:
            backoff = exp_backoff(job.attempts)
            async with self.sf() as sess, sess.begin():
                state = await q.retry(sess, job_id=job.id, worker_id=self.s.worker_id,
                                      error={**err.as_dict(), "tail": tail, "log_uri": log_uri,
                                             "logs": logs},
                                      backoff_s=backoff)
                await q.add_event(sess, job.id, "retry", err.message, "warning",
                                  {"backoff_s": backoff, "state": state})
                # фиксируем хвост stderr в журнале событий — видно сразу в UI
                if tail:
                    await q.add_event(sess, job.id, "stderr", err.message, "error",
                                      {"tail": tail[-30:]})
            JOBS_TOTAL.labels("retry", err.error_class).inc()
            return

        async with self.sf() as sess, sess.begin():
            await q.complete(sess, job_id=job.id, worker_id=self.s.worker_id, state="failed",
                             error={**err.as_dict(), "tail": tail, "log_uri": log_uri,
                                    "logs": logs},
                             progress=ctx.progress if ctx else {})
            if ctx and ctx.asset:
                await sess.execute(text("UPDATE asset SET state='failed' WHERE id=:i"),
                                   {"i": ctx.asset["id"]})
            # полный лог в журнал событий (последние 30 строк)
            if tail:
                await q.add_event(sess, job.id, "stderr", err.message, "error",
                                  {"tail": tail[-30:], "log_uri": log_uri})
            JOBS_TOTAL.labels("failed", err.error_class).inc()
            JOB_DURATION.labels(job.kind, "python", "failed").observe(0)

        # ВАЖНО: НЕ удаляем scratch-папку при падении — полный ytdlp.log/ffmpeg.log
        # нужен для постмортема. Диск ограничен gc() с TTL=86400s в heartbeat-цикле.

    async def _upload_logs(self, ctx: JobContext | None) -> tuple[str | None, dict[str, str]]:
        """Выгружает ВСЕ *.log задачи в log_storage и проставляет job_step.log_uri."""
        if ctx is None:
            return None, {}
        if ctx.logs is not None:
            await ctx.logs.close()
        files = sorted(ctx.workdir.glob("*.log"))
        if not files:
            return None, {}
        # если в задаче явно не указан log_storage — берём дефолт из настроек воркера
        target = ctx.job.params.get("log_storage") or self.s.log_storage
        if not target:
            return None, {}
        uploaded: dict[str, str] = {}
        try:
            be = ctx.registry.by_name(target)
            for path in files:
                rel = f"logs/{ctx.job.id}/{path.name}"
                entry = await be.upload(str(path), rel, overwrite=True)
                uploaded[path.name] = entry.uri
            await self._link_step_logs(ctx, uploaded)
        except Exception as e:  # noqa: BLE001
            log.warning("log_upload_failed", error=str(e))
        if not uploaded:
            return None, {}
        preferred = ("ytdlp.log", "transcode.log", "capture.log")
        first = next((uploaded[n] for n in preferred if n in uploaded),
                     next(iter(uploaded.values())))
        return first, uploaded

    async def _link_step_logs(self, ctx: JobContext, uploaded: dict[str, str]) -> None:
        rows = [(int(name.split("_", 1)[0]), uri) for name, uri in uploaded.items()
                if name.split("_", 1)[0].isdigit()]
        if not rows:
            return
        async with self.sf() as sess, sess.begin():
            for idx, uri in rows:
                await sess.execute(
                    text("UPDATE job_step SET log_uri=:u WHERE job_id=:j AND idx=:i"),
                    {"u": uri, "j": ctx.job.id, "i": idx})

    # -------------------------------- drain --------------------------------
    async def _drain(self) -> None:
        if not self.tasks:
            return
        timeout = (min(60, self.s.drain_timeout_s) if self.s.on_sigterm == "requeue"
                   else self.s.drain_timeout_s)
        log.info("draining", tasks=len(self.tasks), timeout_s=timeout, mode=self.s.on_sigterm)
        done, pending = await asyncio.wait(self.tasks, timeout=timeout)
        if pending:
            log.warning("drain_timeout_cancelling", pending=len(pending))
            for ctx in self.contexts.values():
                ctx.draining = True
                ctx.cancel_reason = "drain"
                ctx.cancel.set()
            await asyncio.wait(pending, timeout=30)

def cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="mf-worker")
    parser.add_argument("--once", action="store_true", help="выйти после первой задачи (отладка)")
    args = parser.parse_args()
    worker = Worker()
    if args.once:
        worker.s.idle_poll_s = 0.2
    asyncio.run(worker.run())

if __name__ == "__main__":
    cli()