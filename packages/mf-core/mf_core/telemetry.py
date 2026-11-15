from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

import orjson
import structlog
from prometheus_client import (
    CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, REGISTRY, generate_latest,
)

from mf_core.config import get_settings

# ------------------------------- metrics -----------------------------------
JOB_DURATION = Histogram(
    "mf_job_duration_seconds", "Длительность задачи", ["kind", "engine", "result"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200, 21600),
)
JOBS_TOTAL = Counter("mf_jobs_total", "Завершённые задачи", ["result", "error_class"])
STEP_DURATION = Histogram("mf_step_duration_seconds", "Длительность шага", ["step", "engine"])
FFMPEG_SPEED = Histogram(
    "mf_ffmpeg_speed_ratio", "Realtime-фактор кодирования", ["engine", "codec"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32),
)
HW_FALLBACK = Counter("mf_hw_fallback_total", "Переходы GPU→CPU", ["reason"])
TRANSFER_BYTES = Counter("mf_transfer_bytes_total", "Трафик хранилищ", ["direction", "kind"])
QUEUE_DEPTH = Gauge("mf_queue_depth", "Глубина очереди", ["state", "gpu"])
WORKER_SLOTS = Gauge("mf_worker_slots", "Слоты воркеров", ["kind", "state"])
GPU_UTIL = Gauge("mf_gpu_utilization", "Утилизация GPU, %", ["index", "engine"])
GPU_MEM = Gauge("mf_gpu_memory_bytes", "Память GPU", ["index", "state"])
LEASE_REAPED = Counter("mf_lease_reaped_total", "Задачи, возвращённые reaper'ом")


def setup_logging() -> None:
    s = get_settings()
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=s.log_level.upper())
    for noisy in ("aiobotocore", "s3fs", "smbprotocol", "asyncio", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    renderer: Any = (
        structlog.processors.JSONRenderer(serializer=lambda obj, **_: orjson.dumps(obj).decode())
        if s.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(s.log_level.upper())
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "mf"):
    return structlog.get_logger(name)


def bind_context(**kw: Any) -> None:
    structlog.contextvars.bind_contextvars(**{k: str(v) for k, v in kw.items() if v is not None})


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


# --------------------- health + metrics HTTP (без зависимостей) --------------
@dataclass
class HealthState:
    healthy: bool = True
    ready: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {"healthy": self.healthy, "ready": self.ready, **self.detail}


async def serve_observability(port: int, state: HealthState) -> asyncio.AbstractServer:
    log = get_logger("mf.obs")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            path = request.split(b" ")[1].decode()
            if path.startswith("/metrics"):
                body, ctype, code = generate_latest(REGISTRY), CONTENT_TYPE_LATEST, "200 OK"
            elif path.startswith(("/healthz", "/readyz")):
                ok = state.healthy if path.startswith("/healthz") else state.ready
                body = orjson.dumps(state.snapshot())
                ctype, code = "application/json", "200 OK" if ok else "503 Service Unavailable"
            else:
                body, ctype, code = b"not found", "text/plain", "404 Not Found"
            writer.write(
                f"HTTP/1.1 {code}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
            )
            await writer.drain()
        except Exception as e:  # noqa: BLE001
            log.debug("obs_request_failed", error=str(e))
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    log.info("observability_listening", port=port)
    return server