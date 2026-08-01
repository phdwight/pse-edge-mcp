"""Machine clients for the client_credentials grant (headless agents).

Revision ID: 0005_machine
Revises: 0004_usage
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_machine"
down_revision = "0004_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `client_type` is the whole security boundary for this feature. /oauth/register is
    # open to anyone, so the right to use client_credentials must not be derivable from
    # anything the registrant supplies — it is this column, writable only by the admin CLI.
    # Existing rows are DCR clients by definition, and the server_default keeps future DCR
    # inserts on the safe side without the application having to remember.
    op.add_column(
        "oauth_clients",
        sa.Column("client_type", sa.String(), nullable=False, server_default="dcr"),
    )
    op.add_column("oauth_clients", sa.Column("client_secret_hash", sa.String()))
    op.add_column("oauth_clients", sa.Column("service_user_id", sa.String()))
    op.add_column("oauth_clients", sa.Column("revoked_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_oauth_clients_service_user_id", "oauth_clients", ["service_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_clients_service_user_id", table_name="oauth_clients")
    op.drop_column("oauth_clients", "revoked_at")
    op.drop_column("oauth_clients", "service_user_id")
    op.drop_column("oauth_clients", "client_secret_hash")
    op.drop_column("oauth_clients", "client_type")
