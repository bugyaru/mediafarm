"""ui: scan_requested_at + индексы для листингов

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.add_column("watch_folder", sa.Column("scan_requested_at", TS))
    op.create_index("ix_job_created_desc", "job", [sa.text("created_at DESC")])
    op.create_index("ix_asset_discovered_desc", "asset", [sa.text("discovered_at DESC")])
    op.create_index("ix_schedule_enabled", "schedule", ["enabled"])


def downgrade() -> None:
    op.drop_index("ix_schedule_enabled", table_name="schedule")
    op.drop_index("ix_asset_discovered_desc", table_name="asset")
    op.drop_index("ix_job_created_desc", table_name="job")
    op.drop_column("watch_folder", "scan_requested_at")