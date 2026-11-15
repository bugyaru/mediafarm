from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from mf_core import queue as q
from mf_core.config import Settings
from mf_core.errors import LeaseLost
from mf_core.media.hwaccel import Hardware
from mf_core.media.probe import MediaInfo
from mf_core.schemas import PresetSpec
from mf_core.storage import StorageRegistry
from mf_core.telemetry import get_logger

log = get_logger("mf.job")

WEIGHTS = {"acquire": 10, "probe": 2, "transcode": 75, "thumbnail": 3,
           "verify": 2, "publish": 8, "notify": 1}


class JobContext:
    def __init__(
            self, *, job: q.ClaimedJob, spec: PresetSpec, settings: Settings,
            registry: StorageRegistry, hw: Hardware, session_factory, workdir: Path,
    ) -> None:
        self.job, self.spec, self.s = job, spec, settings
        self.registry, self.hw, self.sf = registry, hw, session_factory
        self.workdir = workdir
        self.cancel = asyncio.Event()
        self.cancel_reason: str | None = None
        self.lease_lost = False
        self.draining = False

        self.asset: dict[str, Any] | None = None
        self.info: MediaInfo | None = None
        self.input: str | None = None
        self.input_local = False
        self.outputs: dict[str, Any] = {}
        self.artifacts: list[dict[str, Any]] = []
        self.metrics: dict[str, Any] = {}
        self.yt_info: dict[str, Any] = {}

        self._weights = [WEIGHTS.get(st.id, 5) for st in spec.steps]
        self._total_weight = sum(self._weights) or 1
        self._step_idx = 0
        self._last_emit = 0.0
        self.progress: dict[str, Any] = {"pct": 0.0, "step": spec.steps[0].id, "step_idx": 0}

    # ---------------- прогресс ----------------
    def set_step(self, idx: int) -> None:
        self._step_idx = idx
        self._last_emit = 0.0
        self.progress = {"pct": self._base_pct(), "step": self.spec.steps[idx].id,
                         "step_idx": idx, "step_pct": 0.0}

    def _base_pct(self) -> float:
        return round(sum(self._weights[:self._step_idx]) / self._total_weight * 100, 2)

    def on_progress(self, payload: dict[str, Any]) -> None:
        """Синхронный колбэк из run_media/ytdlp: только память, БД пишет heartbeat."""
        now = time.monotonic()
        if now - self._last_emit < self.s.progress_throttle_s and not payload.get("done"):
            return
        self._last_emit = now
        step_pct = payload.get("pct")
        if step_pct is None and payload.get("bytes") and payload.get("total"):
            step_pct = payload["bytes"] / payload["total"] * 100
        step_pct = float(step_pct or 0.0)
        weight = self._weights[self._step_idx]
        overall = self._base_pct() + weight * min(100.0, step_pct) / 100 / self._total_weight * 100
        self.progress = {
            "pct": round(min(99.9, overall), 2),
            "step": self.spec.steps[self._step_idx].id,
            "step_idx": self._step_idx,
            "step_pct": round(step_pct, 2),
            **{k: v for k, v in payload.items() if k in
               ("fps", "speed", "eta_s", "bitrate", "frame", "bytes", "total", "out_time_s")},
        }

    # ---------------- БД ----------------
    async def step_start(self, idx: int, name: str, engine: str | None,
                         cmdline: str | None = None) -> None:
        async with self.sf() as s, s.begin():
            await q.step_start(s, self.job.id, idx, name, engine, cmdline)
            await q.add_event(s, self.job.id, "step_start", name, data={"idx": idx})

    async def step_finish(self, idx: int, state: str, metrics: dict | None = None,
                          error: dict | None = None, log_uri: str | None = None) -> None:
        async with self.sf() as s, s.begin():
            await q.step_finish(s, self.job.id, idx, state, metrics, error, log_uri)

    async def event(self, type_: str, message: str = "", level: str = "info",
                    data: dict | None = None) -> None:
        async with self.sf() as s, s.begin():
            await q.add_event(s, self.job.id, type_, message, level, data)

    async def heartbeat(self) -> None:
        async with self.sf() as s, s.begin():
            cancel = await q.heartbeat(s, job_id=self.job.id, worker_id=self.s.worker_id,
                                       progress=self.progress, lease_s=self.s.lease_s)
        if cancel and not self.cancel.is_set():
            self.cancel_reason = "user"
            self.cancel.set()

    def lose_lease(self) -> None:
        self.lease_lost = True
        self.cancel_reason = "lease"
        self.cancel.set()

    def check_cancel(self) -> None:
        if self.lease_lost:
            raise LeaseLost(f"lease потерян: job {self.job.id}")
        if self.cancel.is_set():
            from mf_core.errors import CancelledByUser
            raise CancelledByUser(self.cancel_reason or "отменено")