"""job_log: полные построчные логи инструментов + NOTIFY job_logs для live-хвоста в UI

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UUID = pg.UUID(as_uuid=True)
TS = sa.TIMESTAMP(timezone=True)

def upgrade() -> None:
    op.create_table(
        "job_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job_id", UUID, sa.ForeignKey("job.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_idx", sa.Integer, nullable=False, server_default=sa.text("-1")),
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("ts", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("tool", sa.String(16), nullable=False, server_default=sa.text("'worker'")),
        sa.Column("stream", sa.String(8), nullable=False, server_default=sa.text("'stdout'")),
        sa.Column("level", sa.String(8), nullable=False, server_default=sa.text("'info'")),
        sa.Column("line", sa.Text, nullable=False),
        sa.CheckConstraint("stream IN ('stdout','stderr','progress','meta')", name="ck_log_stream"),
        sa.CheckConstraint("level IN ('debug','info','warning','error')", name="ck_log_level"),
    )
    op.create_index("ix_log_job_id", "job_log", ["job_id", "id"])
    op.create_index("ix_log_job_step", "job_log", ["job_id", "step_idx", "id"])
    op.create_index("ix_log_ts", "job_log", ["ts"])

    # Один INSERT (пачка строк) → один NOTIFY на задачу: payload маленький,
    # UI дочитывает строки из таблицы по курсору id.
    op.execute("""
        CREATE OR REPLACE FUNCTION mf_job_log_notify() RETURNS trigger AS $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT job_id, max(id) AS last_id, count(*) AS n
                     FROM inserted GROUP BY job_id
            LOOP
                PERFORM pg_notify('job_logs', json_build_object(
                    'id', r.job_id, 'last_id', r.last_id, 'n', r.n)::text);
            END LOOP;
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_job_log_notify
        AFTER INSERT ON job_log
        REFERENCING NEW TABLE AS inserted
        FOR EACH STATEMENT EXECUTE FUNCTION mf_job_log_notify();
    """)

def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_job_log_notify ON job_log")
    op.execute("DROP FUNCTION IF EXISTS mf_job_log_notify()")
    op.drop_index("ix_log_ts", table_name="job_log")
    op.drop_index("ix_log_job_step", table_name="job_log")
    op.drop_index("ix_log_job_id", table_name="job_log")
    op.drop_table("job_log")