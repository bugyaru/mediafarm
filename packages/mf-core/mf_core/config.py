from __future__ import annotations

import socket
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["ui", "watcher", "worker", "migrate", "tool"]
WorkerKind = Literal["cpu", "cuda"]
OnSigterm = Literal["drain", "requeue"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MF_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore", case_sensitive=False,
    )

    # --- общее ---
    role: Role = "tool"
    db_dsn: str = "postgresql+asyncpg://mf:mf@localhost:5432/mf"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_s: int = 30
    db_echo: bool = False
    db_statement_timeout_ms: int = 30_000
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    secret_provider: Literal["env", "file"] = "env"
    secret_cache_ttl_s: int = 60

    # --- worker ---
    worker_id: str = Field(default_factory=socket.gethostname)
    kind: WorkerKind = "cpu"
    cpu_slots: int = 4
    gpu_slots: int = 0
    nvenc_session_limit: int = 0          # 0 = без лимита (L40 / professional)
    gpu_min_free_mem_mb: int = 2000
    gpu_max_mem_fraction: float = 0.9
    lease_s: int = 60
    heartbeat_s: int = 5
    idle_poll_s: float = 1.0
    progress_throttle_s: float = 2.0
    stall_timeout_s: int = 120
    on_sigterm: OnSigterm = "drain"
    drain_timeout_s: int = 1700
    scratch: str = "/scratch"
    scratch_min_free_gb: int = 10
    metrics_port: int = 9100
    max_attempts: int = 3

    # --- media tools ---
    probe_timeout_s: int = 60
    probe_analyze_s: int = 10
    ffmpeg_stop_grace_s: int = 10
    media_threads: int = 0          # 0 = авто (ffmpeg сам)
    ffmpeg_path: str = "/usr/local/bin/ffmpeg"
    ffprobe_path: str = "/usr/local/bin/ffprobe"
    ffmbc_path: str = "/usr/local/bin/ffmbc"
    ffmbc_enabled: bool = True
    ytdlp_path: str = "/usr/local/bin/yt-dlp"
    ytdlp_timeout_s: int = 60
    egress_allow_domains: list[str] = Field(default_factory=list)   # [] = разрешить всё
    egress_block_private_ip: bool = True

    # --- storage ---
    storage_chunk_mb: int = 8
    storage_s3_multipart_mb: int = 64
    storage_s3_concurrency: int = 8
    storage_ftp_max_sessions: int = 4
    storage_smb_timeout_s: int = 30
    storage_local_allow_paths: list[str] = Field(default_factory=list)
    storage_tmp_suffix: str = ".part"

    # --- watcher ---
    watcher_poll_s: int = 30
    watcher_max_backlog: int = 5000
    watcher_inotify: bool = True
    watcher_lock_base: int = 727_100
    watcher_refresh_s: int = 15
    watcher_max_entries: int = 5000
    watcher_archive_interval_s: int = 60
    watcher_lock_retry_s: int = 20
    watcher_error_backoff_s: int = 30
    watcher_stability_ttl_s: int = 3600

    # --- ui ---
    ui_secret_key: str = "change-me-32-bytes-min"
    ui_host: str = "0.0.0.0"
    ui_port: int = 8080
    ui_session_cookie: str = "mf_session"
    ui_session_ttl_s: int = 86_400
    ui_leader_lock_key: int = 727_001
    ui_oidc_issuer: str | None = None
    ui_oidc_client_id: str | None = None
    ui_oidc_client_secret: str | None = None
    ui_bootstrap_user: str = "admin"
    ui_bootstrap_password: str | None = None
    ui_internal_token: str | None = None
    ui_base_url: str = "http://localhost:8080"
    ui_retention_days: int = 90
    ui_reaper_interval_s: int = 10
    ui_schedule_sync_s: int = 15
    ui_ws_max_clients: int = 200

    @field_validator("db_dsn")
    @classmethod
    def _check_dsn(cls, v: str) -> str:
        if not v.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("MF_DB_DSN должен быть PostgreSQL DSN (postgresql+asyncpg://)")
        return v

    @field_validator("gpu_slots")
    @classmethod
    def _gpu_nonneg(cls, v: int) -> int:
        if v < 0:
            raise ValueError("MF_GPU_SLOTS >= 0")
        return v

    @property
    def sync_dsn(self) -> str:
        """DSN для alembic/APScheduler (psycopg3)."""
        return self.db_dsn.replace("postgresql+asyncpg://", "postgresql+psycopg://")

    @property
    def asyncpg_dsn(self) -> str:
        """Чистый DSN для asyncpg (LISTEN/NOTIFY)."""
        return self.db_dsn.replace("postgresql+asyncpg://", "postgresql://")

    @property
    def redacted_dsn(self) -> str:
        p = urlsplit(self.db_dsn)
        if p.password:
            netloc = f"{p.username}:***@{p.hostname}" + (f":{p.port}" if p.port else "")
            return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
        return self.db_dsn

    @property
    def effective_gpu_slots(self) -> int:
        if self.kind != "cuda":
            return 0
        if self.nvenc_session_limit > 0:
            return min(self.gpu_slots, self.nvenc_session_limit)
        return self.gpu_slots


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()