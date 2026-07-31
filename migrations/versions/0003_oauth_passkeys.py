"""Passkeys and OAuth (Phase 5 stage 2).

Revision ID: 0003_oauth
Revises: 0002_auth
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_oauth"
down_revision = "0002_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_sessions",
        sa.Column("sid_hash", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id")),
        sa.Column("email", sa.String()),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("current_challenge", sa.String()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "email_verifications",
        sa.Column("token_hash", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "webauthn_credentials",
        sa.Column("credential_id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("public_key", sa.String(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("aaguid", sa.String()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_webauthn_credentials_user_id", "webauthn_credentials", ["user_id"])
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(), primary_key=True),
        sa.Column("client_name", sa.String(), nullable=False),
        sa.Column("redirect_uris", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "oauth_flows",
        sa.Column("flow_id", sa.String(), primary_key=True),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("state", sa.String()),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column("scope", sa.String()),
        sa.Column("user_id", sa.String()),
        sa.Column("code_hash", sa.String()),
        sa.Column("code_expires_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_oauth_flows_code_hash", "oauth_flows", ["code_hash"])

    # Refresh-token support on the existing token table: which client a token was minted
    # for, and its rotation family (reuse of a revoked family member reveals theft).
    op.add_column("auth_tokens", sa.Column("client_id", sa.String()))
    op.add_column("auth_tokens", sa.Column("family_id", sa.String()))
    op.create_index("ix_auth_tokens_family_id", "auth_tokens", ["family_id"])


def downgrade() -> None:
    op.drop_index("ix_auth_tokens_family_id", table_name="auth_tokens")
    op.drop_column("auth_tokens", "family_id")
    op.drop_column("auth_tokens", "client_id")
    op.drop_index("ix_oauth_flows_code_hash", table_name="oauth_flows")
    op.drop_table("oauth_flows")
    op.drop_table("oauth_clients")
    op.drop_index("ix_webauthn_credentials_user_id", table_name="webauthn_credentials")
    op.drop_table("webauthn_credentials")
    op.drop_table("email_verifications")
    op.drop_table("web_sessions")
