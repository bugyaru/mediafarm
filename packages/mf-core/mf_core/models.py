from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, ForeignKey, Identity, Index, Integer,
    String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TS = TIMESTAMP(timezone=True)

STORAGE_KINDS = ("local", "nfs", "smb", "s3", "ftp")
ASSET_STATES = ("new", "planned", "processing", "done", "failed", "broken", "skipped")
JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled", "retry_scheduled")
STEP_STATES = ("pending", "running", "succeeded", "failed", "skipped")
WORKER_STATES = ("online", "draining", "offline")
USER_ROLES = ("admin", "operator", "viewer")
ARCHIVE_MODES = ("none", "mark", "move")


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True,
        default=uuid.uuid4, server_default=text("gen_random_uuid()"),
    )


def _created() -> Mapped[dt.datetime]:
    return mapped_column(TS, nullable=False, server_default=func.now())


def _updated() -> Mapped[dt.datetime]:
    return mapped_column(TS, nullable=False, server_default=func.now())


class Storage(Base):
    __tablename__ = "storage"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    base_uri: Mapped[str] = mapped_column(Text, nullable=False)
    mount_path: Mapped[str | None] = mapped_column(Text)
    options: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    secret_ref: Mapped[str | None] = mapped_column(String(256))
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()
    __table_args__ = (
        CheckConstraint(f"kind IN {STORAGE_KINDS}", name="ck_storage_kind"),
    )


class Preset(Base):
    __tablename__ = "preset"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    description: Mapped[str | None] = mapped_column(Text)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_preset_name_version"),
        Index("ix_preset_name", "name"),
    )


class WatchFolder(Base):
    __tablename__ = "watch_folder"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    storage_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("storage.id", ondelete="RESTRICT"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    poll_interval_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    stability_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("60"))
    include: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    exclude: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    ready_marker: Mapped[str | None] = mapped_column(String(128))
    parser_id: Mapped[str | None] = mapped_column(String(64))
    parser_opts: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    preset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("preset.id", ondelete="SET NULL"))
    job_template: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    concurrency_key: Mapped[str | None] = mapped_column(String(64))
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    archive_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'none'"))
    archive_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()
    __table_args__ = (
        CheckConstraint(f"archive_mode IN {ARCHIVE_MODES}", name="ck_wf_archive_mode"),
        CheckConstraint("poll_interval_s >= 5", name="ck_wf_poll"),
        CheckConstraint("stability_s >= 0", name="ck_wf_stability"),
    )
    last_scan_at: Mapped[dt.datetime | None] = mapped_column(TS)
    last_error: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class Asset(Base):
    __tablename__ = "asset"
    id: Mapped[uuid.UUID] = _pk()
    identity_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    watch_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("watch_folder.id", ondelete="SET NULL")
    )
    storage_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("storage.id", ondelete="SET NULL"))
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'file'"))
    size: Mapped[int | None] = mapped_column(BigInteger)
    mtime: Mapped[dt.datetime | None] = mapped_column(TS)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(128))
    probe: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset.id", ondelete="SET NULL"))
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'new'"))
    discovered_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()
    __table_args__ = (
        CheckConstraint(f"state IN {ASSET_STATES}", name="ck_asset_state"),
        CheckConstraint("kind IN ('file','url','stream')", name="ck_asset_kind"),
        Index("ix_asset_wf_state", "watch_folder_id", "state"),
        Index("ix_asset_uri", "uri"),
    )
    archived_at: Mapped[dt.datetime | None] = mapped_column(TS)


class Job(Base):
    __tablename__ = "job"
    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'transcode'"))
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))

    preset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("preset.id", ondelete="SET NULL"))
    preset_version: Mapped[int | None] = mapped_column(Integer)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    selector: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    requires_gpu: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    gpu_sessions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cpu_cost: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    concurrency_key: Mapped[str | None] = mapped_column(String(64))
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset.id", ondelete="SET NULL"))
    watch_folder_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("watch_folder.id", ondelete="SET NULL")
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schedule.id", ondelete="SET NULL"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    run_after: Mapped[dt.datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(TS)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    error: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[dt.datetime] = _created()
    started_at: Mapped[dt.datetime | None] = mapped_column(TS)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS)
    updated_at: Mapped[dt.datetime] = _updated()
    __table_args__ = (
        CheckConstraint(f"state IN {JOB_STATES}", name="ck_job_state"),
        CheckConstraint("priority BETWEEN 0 AND 1000", name="ck_job_priority"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_job_attempts"),
        CheckConstraint("cpu_cost >= 1 AND gpu_sessions >= 0", name="ck_job_cost"),
        Index("ix_job_queue", "priority", "run_after", postgresql_where=text("state = 'queued'")),
        Index("ix_job_lease", "lease_expires_at", postgresql_where=text("state = 'running'")),
        Index("ix_job_concurrency", "concurrency_key", postgresql_where=text("state = 'running'")),
        Index("ix_job_worker", "worker_id", postgresql_where=text("state = 'running'")),
        Index("ix_job_state_created", "state", "created_at"),
        Index("ix_job_asset", "asset_id"),
    )


class JobStep(Base):
    __tablename__ = "job_step"
    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), nullable=False)
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    engine: Mapped[str | None] = mapped_column(String(16))
    cmdline: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    error: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    log_uri: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime | None] = mapped_column(TS)
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS)
    __table_args__ = (
        UniqueConstraint("job_id", "idx", name="uq_step_job_idx"),
        CheckConstraint(f"state IN {STEP_STATES}", name="ck_step_state"),
    )


class JobEvent(Base):
    __tablename__ = "job_event"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), nullable=False)
    ts: Mapped[dt.datetime] = _created()
    level: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'info'"))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    __table_args__ = (Index("ix_event_job_ts", "job_id", "ts"), Index("ix_event_ts", "ts"))


class Artifact(Base):
    __tablename__ = "artifact"
    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), nullable=False)
    storage_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("storage.id", ondelete="RESTRICT"), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'output'"))
    size: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(128))
    probe: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[dt.datetime] = _created()
    __table_args__ = (UniqueConstraint("job_id", "uri", name="uq_artifact_job_uri"),)


class Worker(Base):
    __tablename__ = "worker"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    host: Mapped[str | None] = mapped_column(String(256))
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    cpu_slots: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    gpu_slots: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cpu_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    gpu_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'online'"))
    started_at: Mapped[dt.datetime] = _created()
    heartbeat_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()
    __table_args__ = (CheckConstraint(f"state IN {WORKER_STATES}", name="ck_worker_state"),)


class Schedule(Base):
    __tablename__ = "schedule"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    cron: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'UTC'"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    job_template: Mapped[dict] = mapped_column(JSONB, nullable=False)
    misfire_grace_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("300"))
    last_run_at: Mapped[dt.datetime | None] = mapped_column(TS)
    next_run_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()


class AppUser(Base):
    __tablename__ = "app_user"
    id: Mapped[uuid.UUID] = _pk()
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(256))
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'viewer'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_login_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = _updated()
    __table_args__ = (CheckConstraint(f"role IN {USER_ROLES}", name="ck_user_role"),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[dt.datetime] = _created()
    actor: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    __table_args__ = (Index("ix_audit_ts", "ts"), Index("ix_audit_target", "target_type", "target_id"))


class AppSetting(Base):
    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[dt.datetime] = _updated()


TOUCH_TABLES = (
    "storage", "preset", "watch_folder", "asset", "job", "worker",
    "schedule", "app_user", "app_setting",
)
NOTIFY_CHANNEL = "job_events"