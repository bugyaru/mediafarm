"""watcher: archived_at, last_scan_at, last_error

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.add_column("asset", sa.Column("archived_at", TS))
    op.add_column("watch_folder", sa.Column("last_scan_at", TS))
    op.add_column("watch_folder", sa.Column(
        "last_error", pg.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.create_index(
        "ix_asset_archive", "asset", ["watch_folder_id", "state"],
        postgresql_where=sa.text("archived_at IS NULL AND state = 'done'"),
    )


def downgrade() -> None:
    op.drop_index("ix_asset_archive", table_name="asset")
    op.drop_column("watch_folder", "last_error")
    op.drop_column("watch_folder", "last_scan_at")
    op.drop_column("asset", "archived_at")