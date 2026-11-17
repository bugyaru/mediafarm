"""initial schema: storage/preset/watch_folder/asset/job/... + triggers

Revision ID: 0001
Revises:
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSONB = pg.JSONB
UUID = pg.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)


def _id():
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created(name="created_at"):
    return sa.Column(name, TS, nullable=False, server_default=sa.func.now())


def _updated():
    return sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now())


def _json(name, default="{}"):
    return sa.Column(name, JSONB, nullable=False, server_default=sa.text(f"'{default}'::jsonb"))


def _bool(name, default=False):
    return sa.Column(name, sa.Boolean, nullable=False, server_default=sa.text(str(default).lower()))


def _int(name, default=0):
    return sa.Column(name, sa.Integer, nullable=False, server_default=sa.text(str(default)))


TOUCH_TABLES = (
    "storage", "preset", "watch_folder", "asset", "job", "worker",
    "schedule", "app_user", "app_setting",
)

upgrade_order = (
    "storage", "preset", "watch_folder", "schedule", "asset", "job",
    "job_step", "job_event", "artifact", "worker", "app_user", "audit_log", "app_setting",
)


def upgrade() -> None:
    # ---------------- storage ----------------
    op.create_table(
        "storage",
        _id(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("base_uri", sa.Text, nullable=False),
        sa.Column("mount_path", sa.Text),
        _json("options"),
        sa.Column("secret_ref", sa.String(256)),
        _bool("read_only", False),
        _bool("enabled", True),
        _created(), _updated(),
        sa.CheckConstraint("kind IN ('local','nfs','smb','s3','ftp')", name="ck_storage_kind"),
    )

    # ---------------- preset ----------------
    op.create_table(
        "preset",
        _id(),
        sa.Column("name", sa.String(128), nullable=False),
        _int("version", 1),
        sa.Column("description", sa.Text),
        sa.Column("spec", JSONB, nullable=False),
        _bool("enabled", True),
        _created(), _updated(),
        sa.UniqueConstraint("name", "version", name="uq_preset_name_version"),
    )
    op.create_index("ix_preset_name", "preset", ["name"])

    # ---------------- watch_folder ----------------
    op.create_table(
        "watch_folder",
        _id(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("storage_id", UUID, sa.ForeignKey("storage.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("path", sa.Text, nullable=False, server_default=sa.text("''")),
        _bool("recursive", True), _bool("enabled", True),
        _int("poll_interval_s", 30), _int("stability_s", 60),
        _json("include", "[]"), _json("exclude", "[]"),
        sa.Column("ready_marker", sa.String(128)),
        sa.Column("parser_id", sa.String(64)),
        _json("parser_opts"),
        sa.Column("preset_id", UUID, sa.ForeignKey("preset.id", ondelete="SET NULL")),
        _json("job_template"),
        sa.Column("concurrency_key", sa.String(64)),
        _int("max_concurrency", 0),
        sa.Column("archive_mode", sa.String(16), nullable=False, server_default=sa.text("'none'")),
        sa.Column("archive_path", sa.Text),
        _created(), _updated(),
        sa.CheckConstraint("archive_mode IN ('none','mark','move')", name="ck_wf_archive_mode"),
        sa.CheckConstraint("poll_interval_s >= 5", name="ck_wf_poll"),
        sa.CheckConstraint("stability_s >= 0", name="ck_wf_stability"),
    )

    # ---------------- schedule ----------------
    op.create_table(
        "schedule",
        _id(),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("cron", sa.String(64), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default=sa.text("'UTC'")),
        _bool("enabled", True),
        sa.Column("job_template", JSONB, nullable=False),
        _int("misfire_grace_s", 300),
        sa.Column("last_run_at", TS), sa.Column("next_run_at", TS),
        _created(), _updated(),
    )

    # ---------------- asset ----------------
    op.create_table(
        "asset",
        _id(),
        sa.Column("identity_key", sa.String(64), nullable=False, unique=True),
        sa.Column("watch_folder_id", UUID, sa.ForeignKey("watch_folder.id", ondelete="SET NULL")),
        sa.Column("storage_id", UUID, sa.ForeignKey("storage.id", ondelete="SET NULL")),
        sa.Column("uri", sa.Text, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default=sa.text("'file'")),
        sa.Column("size", sa.BigInteger),
        sa.Column("mtime", TS),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("checksum", sa.String(128)),
        _json("probe"), _json("meta"),
        sa.Column("parent_id", UUID, sa.ForeignKey("asset.id", ondelete="SET NULL")),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'new'")),
        _created("discovered_at"), _updated(),
        sa.CheckConstraint(
            "state IN ('new','planned','processing','done','failed','broken','skipped')",
            name="ck_asset_state"),
        sa.CheckConstraint("kind IN ('file','url','stream')", name="ck_asset_kind"),
    )
    op.create_index("ix_asset_wf_state", "asset", ["watch_folder_id", "state"])
    op.create_index("ix_asset_uri", "asset", ["uri"])

    # ---------------- job ----------------
    op.create_table(
        "job",
        _id(),
        sa.Column("kind", sa.String(24), nullable=False, server_default=sa.text("'transcode'")),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'queued'")),
        _int("priority", 100),
        sa.Column("preset_id", UUID, sa.ForeignKey("preset.id", ondelete="SET NULL")),
        sa.Column("preset_version", sa.Integer),
        _json("spec"), _json("params"), _json("selector"),
        _bool("requires_gpu", False),
        _int("gpu_sessions", 0), _int("cpu_cost", 1),
        sa.Column("concurrency_key", sa.String(64)),
        _int("concurrency_limit", 0),
        sa.Column("asset_id", UUID, sa.ForeignKey("asset.id", ondelete="SET NULL")),
        sa.Column("watch_folder_id", UUID, sa.ForeignKey("watch_folder.id", ondelete="SET NULL")),
        sa.Column("schedule_id", UUID, sa.ForeignKey("schedule.id", ondelete="SET NULL")),
        sa.Column("idempotency_key", sa.String(128), unique=True),
        _int("attempts", 0), _int("max_attempts", 3),
        sa.Column("run_after", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("lease_expires_at", TS),
        _bool("cancel_requested", False),
        _json("progress"), _json("result"), _json("error"),
        sa.Column("created_by", sa.String(128)),
        _created(), sa.Column("started_at", TS), sa.Column("finished_at", TS), _updated(),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed','cancelled','retry_scheduled')",
            name="ck_job_state"),
        sa.CheckConstraint("priority BETWEEN 0 AND 1000", name="ck_job_priority"),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_job_attempts"),
        sa.CheckConstraint("cpu_cost >= 1 AND gpu_sessions >= 0", name="ck_job_cost"),
    )
    op.create_index("ix_job_queue", "job", ["priority", "run_after"],
                    postgresql_where=sa.text("state = 'queued'"))
    op.create_index("ix_job_lease", "job", ["lease_expires_at"],
                    postgresql_where=sa.text("state = 'running'"))
    op.create_index("ix_job_concurrency", "job", ["concurrency_key"],
                    postgresql_where=sa.text("state = 'running'"))
    op.create_index("ix_job_worker", "job", ["worker_id"],
                    postgresql_where=sa.text("state = 'running'"))
    op.create_index("ix_job_state_created", "job", ["state", "created_at"])
    op.create_index("ix_job_asset", "job", ["asset_id"])

    # ---------------- job_step ----------------
    op.create_table(
        "job_step",
        _id(),
        sa.Column("job_id", UUID, sa.ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idx", sa.Integer, nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("engine", sa.String(16)),
        sa.Column("cmdline", sa.Text),
        _json("metrics"), _json("error"),
        sa.Column("log_uri", sa.Text),
        sa.Column("started_at", TS), sa.Column("finished_at", TS),
        sa.UniqueConstraint("job_id", "idx", name="uq_step_job_idx"),
        sa.CheckConstraint("state IN ('pending','running','succeeded','failed','skipped')",
                           name="ck_step_state"),
    )

    # ---------------- job_event ----------------
    op.create_table(
        "job_event",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job_id", UUID, sa.ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
        _created("ts"),
        sa.Column("level", sa.String(16), nullable=False, server_default=sa.text("'info'")),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("message", sa.Text),
        _json("data"),
    )
    op.create_index("ix_event_job_ts", "job_event", ["job_id", "ts"])
    op.create_index("ix_event_ts", "job_event", ["ts"])

    # ---------------- artifact ----------------
    op.create_table(
        "artifact",
        _id(),
        sa.Column("job_id", UUID, sa.ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("storage_id", UUID, sa.ForeignKey("storage.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("uri", sa.Text, nullable=False),
        sa.Column("role", sa.String(24), nullable=False, server_default=sa.text("'output'")),
        sa.Column("size", sa.BigInteger),
        sa.Column("checksum", sa.String(128)),
        _json("probe"), _json("meta"),
        _created(),
        sa.UniqueConstraint("job_id", "uri", name="uq_artifact_job_uri"),
    )

    # ---------------- worker ----------------
    op.create_table(
        "worker",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("host", sa.String(256)),
        _json("labels"), _json("capabilities"),
        _int("cpu_slots", 0), _int("gpu_slots", 0), _int("cpu_used", 0), _int("gpu_used", 0),
        sa.Column("state", sa.String(16), nullable=False, server_default=sa.text("'online'")),
        _created("started_at"), _created("heartbeat_at"), _updated(),
        sa.CheckConstraint("state IN ('online','draining','offline')", name="ck_worker_state"),
    )

    # ---------------- app_user / audit / settings ----------------
    op.create_table(
        "app_user",
        _id(),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(256)),
        sa.Column("external_id", sa.String(128), unique=True),
        sa.Column("role", sa.String(16), nullable=False, server_default=sa.text("'viewer'")),
        _bool("is_active", True),
        sa.Column("last_login_at", TS),
        _created(), _updated(),
        sa.CheckConstraint("role IN ('admin','operator','viewer')", name="ck_user_role"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        _created("ts"),
        sa.Column("actor", sa.String(128)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32)),
        sa.Column("target_id", sa.String(64)),
        sa.Column("ip", sa.String(64)),
        _json("data"),
    )
    op.create_index("ix_audit_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_target", "audit_log", ["target_type", "target_id"])
    op.create_table(
        "app_setting",
        sa.Column("key", sa.String(64), primary_key=True),
        _json("value"), _updated(),
    )

    # ---------------- триггеры ----------------
    op.execute("""
        CREATE OR REPLACE FUNCTION mf_touch_updated_at() RETURNS trigger AS $$
        BEGIN NEW.updated_at := now(); RETURN NEW; END
        $$ LANGUAGE plpgsql;
    """)
    for t in TOUCH_TABLES:
        op.execute(f"""
            CREATE TRIGGER trg_{t}_touch BEFORE UPDATE ON {t}
            FOR EACH ROW EXECUTE FUNCTION mf_touch_updated_at();
        """)

    # NOTIFY для UI. Payload ограничен 8000 байт -> режем progress/error при переполнении.
    # 1. Создаем функцию
    op.execute("""
        CREATE OR REPLACE FUNCTION mf_job_notify() RETURNS trigger AS $$
        DECLARE payload text;
        BEGIN
            payload := json_build_object(
                'id', NEW.id, 'state', NEW.state, 'progress', NEW.progress,
                'attempts', NEW.attempts, 'worker_id', NEW.worker_id,
                'error', NEW.error, 'updated_at', NEW.updated_at
            )::text;
            IF octet_length(payload) > 7000 THEN
                payload := json_build_object(
                    'id', NEW.id, 'state', NEW.state,
                    'progress', jsonb_build_object('pct', NEW.progress->'pct'),
                    'attempts', NEW.attempts, 'worker_id', NEW.worker_id,
                    'truncated', true, 'updated_at', NEW.updated_at
                )::text;
            END IF;
            PERFORM pg_notify('job_events', payload);
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)

    # 2. Создаем триггер
    op.execute("""
        CREATE TRIGGER trg_job_notify
        AFTER INSERT OR UPDATE OF state, progress, error ON job
        FOR EACH ROW EXECUTE FUNCTION mf_job_notify();
    """)

def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_job_notify ON job")
    op.execute("DROP FUNCTION IF EXISTS mf_job_notify()")
    for t in TOUCH_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_touch ON {t}")
    op.execute("DROP FUNCTION IF EXISTS mf_touch_updated_at()")
    for t in reversed(upgrade_order):
        op.drop_table(t)