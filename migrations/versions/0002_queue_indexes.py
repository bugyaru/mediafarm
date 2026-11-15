"""queue index ordering + worker heartbeat index

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ORDER BY priority DESC, run_after ASC не обслуживается ASC-индексом
    op.drop_index("ix_job_queue", table_name="job")
    op.create_index(
        "ix_job_queue", "job",
        [sa.text("priority DESC"), sa.text("run_after ASC"), sa.text("id ASC")],
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index("ix_worker_heartbeat", "worker", ["heartbeat_at"])
    # для поиска «зависших» отмен, которые воркер ещё не забрал
    op.create_index(
        "ix_job_cancel", "job", ["id"],
        postgresql_where=sa.text("cancel_requested AND state = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("ix_job_cancel", table_name="job")
    op.drop_index("ix_worker_heartbeat", table_name="worker")
    op.drop_index("ix_job_queue", table_name="job")
    op.create_index(
        "ix_job_queue", "job", ["priority", "run_after"],
        postgresql_where=sa.text("state = 'queued'"),
    )