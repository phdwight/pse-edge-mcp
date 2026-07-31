"""Accounts and bearer tokens (Phase 5 stage 1).

Revision ID: 0002_auth
Revises: 0001_initial
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_auth"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Accounts. No password column by design — registration is passkey-based (plan §6);
    # until that ships, accounts come from the admin CLI. Nullable quota columns are the
    # per-user overrides; null means "use the configured default".
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column("quota_per_minute", sa.Integer()),
        sa.Column("quota_per_day", sa.Integer()),
    )

    # Bearer tokens, stored only as SHA-256 hashes — plaintext is shown once at issue
    # time, so a database leak does not leak usable credentials.
    op.create_table(
        "auth_tokens",
        sa.Column("token_hash", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("note", sa.String()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_auth_tokens_user_id", "auth_tokens", ["user_id"])
    # For the eventual expired-token sweep; harmless meanwhile.
    op.create_index("ix_auth_tokens_expires_at", "auth_tokens", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_tokens_expires_at", table_name="auth_tokens")
    op.drop_index("ix_auth_tokens_user_id", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_table("users")
