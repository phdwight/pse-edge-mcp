"""Initial schema: shared cache plus the opportunistic archive.

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The freeze-policy cache, made shared and durable. No TTL column by design: freshness
    # is decided by the market calendar at read time (plan §5a), so a row records only
    # when it was fetched.
    op.create_table(
        "cache_entries",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Archive: daily bars, accrued only from fetches a user already triggered (plan §6a).
    op.create_table(
        "eod_bars",
        sa.Column("company_id", sa.String(), primary_key=True),
        sa.Column("security_id", sa.String(), primary_key=True),
        sa.Column("trade_date", sa.Date(), primary_key=True),
        sa.Column("symbol", sa.String()),
        sa.Column("open", sa.Float()),
        sa.Column("high", sa.Float()),
        sa.Column("low", sa.Float()),
        sa.Column("close", sa.Float()),
        sa.Column("value", sa.Float()),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Archive: every disclosure we have ever seen, keyed by Edge's own natural key.
    op.create_table(
        "disclosures",
        sa.Column("edge_no", sa.String(), primary_key=True),
        sa.Column("company_id", sa.String()),
        sa.Column("company_name", sa.String()),
        sa.Column("template", sa.String()),
        sa.Column("announced_at", sa.DateTime(timezone=True)),
        sa.Column("pse_form_number", sa.String()),
        sa.Column("circular_number", sa.String()),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # The two ways the archive gets queried: "what did this company file" and
    # "what was filed around this time".
    op.create_index("ix_disclosures_company_id", "disclosures", ["company_id"])
    op.create_index("ix_disclosures_announced_at", "disclosures", ["announced_at"])


def downgrade() -> None:
    op.drop_index("ix_disclosures_announced_at", table_name="disclosures")
    op.drop_index("ix_disclosures_company_id", table_name="disclosures")
    op.drop_table("disclosures")
    op.drop_table("eod_bars")
    op.drop_table("cache_entries")
