from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from mf_core.errors import HWAccelError, LeaseLost
from mf_core.schemas import StepSpec
from mf_core.telemetry import HW_FALLBACK, STEP_DURATION, get_logger
from mf_core.util import mask
from worker.context import JobContext
from worker.steps import HANDLERS

log = get_logger("mf.pipeline")
Handler = Callable[[StepSpec, JobContext], Awaitable[dict[str, Any]]]

class Pipeline:
    def __init__(self, ctx: JobContext, handlers: dict[str, Handler] | None = None) -> None:
        self.ctx = ctx
        self.handlers = handlers or HANDLERS

    async def run(self) -> dict[str, Any]:
        ctx = self.ctx
        for idx, step in enumerate(ctx.spec.steps):
            ctx.check_cancel()
            ctx.set_step(idx)
            handler = self.handlers.get(step.id)
            if handler is None:
                await ctx.step_start(idx, step.id, step.engine)
                await ctx.step_finish(idx, "skipped", {"reason": "no handler"})
                await ctx.flush_logs()
                continue

            await ctx.step_start(idx, step.id, step.engine)
            started = time.monotonic()
            try:
                result = await self._run_with_fallback(idx, step, handler)
            except LeaseLost:
                await ctx.flush_logs()
                raise
            except Exception as e:
                err = getattr(e, "as_dict", lambda: {"message": str(e)})()
                ctx.log(f"шаг {step.id} упал: {err.get('message', e)}", "error")
                await ctx.flush_logs()
                await ctx.step_finish(idx, "failed", error=err)
                raise
            elapsed = time.monotonic() - started
            STEP_DURATION.labels(step.id, step.engine or "python").observe(elapsed)
            ctx.log(f"шаг {step.id} завершён за {elapsed:.2f} c")
            await ctx.flush_logs()
            await ctx.step_finish(idx, "succeeded", {**result, "elapsed_s": round(elapsed, 2)}
            if isinstance(result, dict) else {"elapsed_s": elapsed})
            ctx.outputs[step.id] = result
            log.info("step_done", step=step.id, elapsed_s=round(elapsed, 2))

        await ctx.flush_logs()
        return {"artifacts": [_serialize(a) for a in ctx.artifacts], "metrics": ctx.metrics,
                "output": ctx.outputs.get("publish") or ctx.outputs.get("transcode")}

    async def _run_with_fallback(self, idx: int, step: StepSpec, handler: Handler) -> dict:
        try:
            return await handler(step, self.ctx)
        except HWAccelError as e:
            if not (step.fallback_to_cpu and step.id == "transcode"):
                raise
            HW_FALLBACK.labels(e.details.get("reason", "hwaccel")).inc()
            self.ctx.log(f"CUDA недоступна ({e}) — перехожу на CPU", "warning")
            await self.ctx.event("hw_fallback", str(e), level="warning",
                                 data={"reason": e.details})
            cpu_step = step.model_copy(update={"hwaccel": "cpu"})
            log.warning("hw_fallback_to_cpu", step=step.id, error=str(e))
            return await handler(cpu_step, self.ctx)

def _serialize(artifact: dict) -> dict:
    out = dict(artifact)
    if "storage_id" in out:
        out["storage_id"] = str(out["storage_id"])
    return out