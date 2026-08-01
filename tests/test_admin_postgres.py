"""Admin CLI operations against a real migrated Postgres.

The prize test here is the full circle: a token issued by the CLI authenticates through
`PostgresAuthStore` + `TokenService` — the exact production path — and stops
authenticating once revoked or its user is disabled.
"""

from __future__ import annotations

import pytest

from pse_edge_mcp.admin import (
    AdminError,
    create_user,
    disable_user,
    issue_token,
    list_users,
    revoke_token,
    set_quota,
)
from pse_edge_mcp.auth import TokenService
from pse_edge_mcp.auth_store import PostgresAuthStore

pytestmark = pytest.mark.postgres


def service(engine, *, cache_ttl: float = 0.0) -> TokenService:
    """cache_ttl=0 by default so revocations are visible immediately in tests."""
    return TokenService(PostgresAuthStore(engine), cache_ttl_sec=cache_ttl)


async def test_issued_token_authenticates_through_the_real_store(pg_engine):
    await create_user(pg_engine, "Alice@Example.COM ")
    token = await issue_token(pg_engine, "alice@example.com", note="laptop")

    context = await service(pg_engine).authenticate(token)

    assert context is not None
    assert context.email == "alice@example.com"  # normalised at creation
    assert (context.quota_per_minute, context.quota_per_day) == (60, 2000)


async def test_quota_overrides_flow_from_cli_to_auth_context(pg_engine):
    await create_user(pg_engine, "bob@example.com", quota_per_minute=600)
    await set_quota(pg_engine, "bob@example.com", per_day=50_000)
    token = await issue_token(pg_engine, "bob@example.com")

    context = await service(pg_engine).authenticate(token)

    assert context is not None
    assert (context.quota_per_minute, context.quota_per_day) == (600, 50_000)


async def test_revoked_token_stops_authenticating(pg_engine):
    await create_user(pg_engine, "carol@example.com")
    token = await issue_token(pg_engine, "carol@example.com")
    assert await service(pg_engine).authenticate(token) is not None

    assert await revoke_token(pg_engine, token) is True
    assert await service(pg_engine).authenticate(token) is None
    assert await revoke_token(pg_engine, token) is False, "already revoked: nothing matched"


async def test_disable_user_revokes_every_active_token(pg_engine):
    await create_user(pg_engine, "dave@example.com")
    first = await issue_token(pg_engine, "dave@example.com")
    second = await issue_token(pg_engine, "dave@example.com")

    revoked = await disable_user(pg_engine, "dave@example.com")

    assert revoked == 2
    assert await service(pg_engine).authenticate(first) is None
    assert await service(pg_engine).authenticate(second) is None
    with pytest.raises(AdminError, match="disabled"):
        await issue_token(pg_engine, "dave@example.com")


async def test_duplicate_email_and_unknown_user_fail_loudly(pg_engine):
    await create_user(pg_engine, "erin@example.com")
    with pytest.raises(AdminError, match="already exists"):
        await create_user(pg_engine, "ERIN@example.com")  # same address, different case
    with pytest.raises(AdminError, match="no user"):
        await issue_token(pg_engine, "nobody@example.com")
    with pytest.raises(AdminError, match="does not look like an email"):
        await create_user(pg_engine, "not-an-email")


async def test_list_users_reports_state_and_active_token_counts(pg_engine):
    await create_user(pg_engine, "frank@example.com")
    await issue_token(pg_engine, "frank@example.com")
    await issue_token(pg_engine, "frank@example.com")
    await create_user(pg_engine, "grace@example.com")
    await disable_user(pg_engine, "grace@example.com")

    rows = {row["email"]: row for row in await list_users(pg_engine)}

    assert rows["frank@example.com"]["active_tokens"] == 2
    assert rows["frank@example.com"]["disabled_at"] is None
    assert rows["grace@example.com"]["disabled_at"] is not None
    assert rows["grace@example.com"]["active_tokens"] == 0


async def test_deleting_a_machine_service_account_is_refused(pg_engine):
    """Proved broken before fixed: erasing the service user behind a machine client leaves
    `oauth_clients.service_user_id` dangling — the client keeps its secret and is NOT
    revoked, so its next token mint hits the auth_tokens FK and the token endpoint 500s.
    The erasure safety net cannot catch this: it walks columns named `user_id`, and
    `service_user_id` dodges it by name. So the CLI refuses, pointing at the operation
    that does the whole job: revoke-machine-client revokes the client, its outstanding
    tokens, AND disables the account, atomically.
    """
    from pse_edge_mcp.admin import create_machine_client, delete_user_by_email

    created = await create_machine_client(pg_engine, "erase-refusal-app")

    with pytest.raises(AdminError, match="revoke-machine-client"):
        await delete_user_by_email(pg_engine, created["service_user"])

    # And the client still works: the refusal protected it.
    from pse_edge_mcp.oauth import OAuthService

    minted = await OAuthService(pg_engine).exchange(
        {
            "grant_type": "client_credentials",
            "client_id": created["client_id"],
            "client_secret": created["client_secret"],
        }
    )
    assert minted["token_type"] == "Bearer"
