"""Usage audit log with retention (plan §6a privacy compliance).

Revision ID: 0004_usage
Revises: 0003_oauth
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_usage"
down_revision = "0003_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Aggregated per user-hour rather than per request: less data held (itself a §6a
    # requirement), no hot-path write, and retention becomes an indexed range delete.
    op.create_table(
        "usage_events",
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("hour", sa.Integer(), primary_key=True),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # The retention purge deletes by day; the index is what keeps it cheap.
    op.create_index("ix_usage_events_day", "usage_events", ["day"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_day", table_name="usage_events")
    op.drop_table("usage_events")
