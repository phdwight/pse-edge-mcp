"""OAuth 2.1 server rules, against a real migrated Postgres.

Every test here corresponds to a rule in `oauth.py`'s docstring. They are written as
attacks where possible — the point is not that the happy path works but that the wrong
paths are closed.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

from pse_edge_mcp.oauth import (
    FatalAuthorizeError,
    OAuthError,
    OAuthService,
    RedirectAuthorizeError,
)

pytestmark = pytest.mark.postgres

REDIRECT = "https://client.example/callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


async def make_user(engine, email: str = "u@example.com") -> str:
    from pse_edge_mcp.admin import create_user

    return await create_user(engine, email)


async def register(service: OAuthService, uris: list[str] | None = None) -> str:
    result = await service.register_client(
        {"client_name": "Test Client", "redirect_uris": uris or [REDIRECT]}
    )
    return result["client_id"]


def authorize_params(client_id: str, challenge: str, **overrides) -> dict[str, str]:
    return {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "xyz",
        **overrides,
    }


# --- dynamic client registration ---------------------------------------------


async def test_registration_issues_a_public_client_with_no_secret(pg_engine):
    service = OAuthService(pg_engine)
    result = await service.register_client({"client_name": "Claude", "redirect_uris": [REDIRECT]})
    assert result["client_id"]
    assert result["token_endpoint_auth_method"] == "none"
    # There must be no secret at all: nothing to leak, nothing to verify.
    assert "client_secret" not in result


@pytest.mark.parametrize(
    "uri",
    [
        "http://evil.example/cb",  # plain http off-loopback
        "https://client.example/cb#frag",  # fragments are forbidden
        "not-a-url",
        "javascript:alert(1)",
    ],
)
async def test_registration_rejects_unacceptable_redirect_uris(pg_engine, uri):
    # Assert on the RFC 6749 error *code* — that is the machine-readable contract a
    # client branches on; the human description is free to change.
    with pytest.raises(OAuthError) as caught:
        await OAuthService(pg_engine).register_client({"redirect_uris": [uri]})
    assert caught.value.error == "invalid_redirect_uri"


async def test_registration_allows_http_on_loopback_for_native_clients(pg_engine):
    result = await OAuthService(pg_engine).register_client(
        {"redirect_uris": ["http://127.0.0.1:33418/callback"]}
    )
    assert result["client_id"]


# --- authorize: the open-redirector rules ------------------------------------


async def test_unknown_client_is_fatal_and_never_redirects(pg_engine):
    """Redirecting on an unvalidated target would make this an open redirector."""
    _, challenge = pkce()
    with pytest.raises(FatalAuthorizeError):
        await OAuthService(pg_engine).begin_authorize(authorize_params("no-such-client", challenge))


async def test_unregistered_redirect_uri_is_fatal_and_never_redirects(pg_engine):
    service = OAuthService(pg_engine)
    client_id = await register(service)
    _, challenge = pkce()
    with pytest.raises(FatalAuthorizeError):
        await service.begin_authorize(
            authorize_params(client_id, challenge, redirect_uri="https://attacker.example/cb")
        )


async def test_redirect_uri_matching_is_exact_not_prefix(pg_engine):
    """A registered prefix must not authorise a longer attacker-controlled path."""
    service = OAuthService(pg_engine)
    client_id = await register(service, ["https://client.example/callback"])
    _, challenge = pkce()
    for attempt in [
        "https://client.example/callback/../evil",
        "https://client.example/callbackevil",
        "https://client.example/callback?x=1",
    ]:
        with pytest.raises(FatalAuthorizeError):
            await service.begin_authorize(
                authorize_params(client_id, challenge, redirect_uri=attempt)
            )


async def test_errors_on_a_validated_redirect_go_back_via_redirect_with_state(pg_engine):
    service = OAuthService(pg_engine)
    client_id = await register(service)
    with pytest.raises(RedirectAuthorizeError) as caught:
        await service.begin_authorize(
            authorize_params(client_id, "", response_type="token")  # implicit flow
        )
    query = parse_qs(urlparse(caught.value.redirect_url).query)
    assert query["error"] == ["unsupported_response_type"]
    assert query["state"] == ["xyz"], "state must round-trip so the client can correlate"


async def test_pkce_is_mandatory_and_plain_is_refused(pg_engine):
    service = OAuthService(pg_engine)
    client_id = await register(service)
    _, challenge = pkce()

    with pytest.raises(RedirectAuthorizeError) as missing:
        await service.begin_authorize(authorize_params(client_id, ""))
    assert "code_challenge" in missing.value.redirect_url

    with pytest.raises(RedirectAuthorizeError) as plain:
        await service.begin_authorize(
            authorize_params(client_id, challenge, code_challenge_method="plain")
        )
    assert "S256" in plain.value.redirect_url


# --- code exchange -----------------------------------------------------------


async def complete_authorize(engine, service: OAuthService) -> tuple[str, str, str]:
    """Register, authorize, consent — returns (client_id, code, verifier)."""
    client_id = await register(service)
    verifier, challenge = pkce()
    flow = await service.begin_authorize(authorize_params(client_id, challenge))
    user_id = await make_user(engine, f"{secrets.token_hex(4)}@example.com")
    redirect_url = await service.issue_code(flow.flow_id, user_id)
    code = parse_qs(urlparse(redirect_url).query)["code"][0]
    return client_id, code, verifier


async def test_full_code_exchange_returns_a_usable_token_pair(pg_engine):
    service = OAuthService(pg_engine)
    client_id, code, verifier = await complete_authorize(pg_engine, service)

    tokens = await service.exchange(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
        }
    )

    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"].startswith("pse_")
    assert tokens["refresh_token"].startswith("pse_")
    assert tokens["expires_in"] == 1800


async def test_code_is_single_use(pg_engine):
    service = OAuthService(pg_engine)
    client_id, code, verifier = await complete_authorize(pg_engine, service)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client_id,
        "redirect_uri": REDIRECT,
    }
    await service.exchange(form)
    with pytest.raises(OAuthError, match="already used"):
        await service.exchange(form)


async def test_wrong_pkce_verifier_is_refused(pg_engine):
    service = OAuthService(pg_engine)
    client_id, code, _ = await complete_authorize(pg_engine, service)
    with pytest.raises(OAuthError, match="PKCE"):
        await service.exchange(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": secrets.token_urlsafe(64)[:96],
                "client_id": client_id,
                "redirect_uri": REDIRECT,
            }
        )


async def test_mismatched_client_or_redirect_is_refused(pg_engine):
    service = OAuthService(pg_engine)
    client_id, code, verifier = await complete_authorize(pg_engine, service)
    base = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client_id,
        "redirect_uri": REDIRECT,
    }
    with pytest.raises(OAuthError, match="client_id"):
        await service.exchange({**base, "client_id": "someone-else"})


async def test_consent_cannot_be_replayed_to_mint_a_second_code(pg_engine):
    service = OAuthService(pg_engine)
    client_id = await register(service)
    _, challenge = pkce()
    flow = await service.begin_authorize(authorize_params(client_id, challenge))
    user_id = await make_user(pg_engine, "replay@example.com")

    await service.issue_code(flow.flow_id, user_id)
    with pytest.raises(OAuthError, match="expired or already used"):
        await service.issue_code(flow.flow_id, user_id)


# --- refresh rotation --------------------------------------------------------


async def test_refresh_rotates_and_returns_a_new_pair(pg_engine):
    service = OAuthService(pg_engine)
    client_id, code, verifier = await complete_authorize(pg_engine, service)
    first = await service.exchange(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
        }
    )

    second = await service.exchange(
        {
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        }
    )

    assert second["refresh_token"] != first["refresh_token"], "tokens must rotate"
    assert second["access_token"] != first["access_token"]


async def test_refresh_revokes_the_previous_access_token(pg_engine):
    """Rotation retires the whole pair: the access token minted alongside the spent
    refresh token must not outlive the rotation it belongs to."""
    from sqlalchemy import select

    from pse_edge_mcp.auth import hash_token
    from pse_edge_mcp.db import auth_tokens

    service = OAuthService(pg_engine)
    client_id, code, verifier = await complete_authorize(pg_engine, service)
    first = await service.exchange(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
        }
    )

    await service.exchange(
        {
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        }
    )

    async with pg_engine.connect() as conn:
        row = (
            await conn.execute(
                select(auth_tokens.c.revoked_at).where(
                    auth_tokens.c.token_hash == hash_token(first["access_token"])
                )
            )
        ).first()
    assert row.revoked_at is not None, "the old access token must die with its refresh token"


async def test_minting_purges_rows_that_have_expired(pg_engine):
    """Every mint sweeps expired rows — nothing else deletes them, and without the sweep
    the table only ever grows."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import insert, select

    from pse_edge_mcp.db import auth_tokens

    service = OAuthService(pg_engine)
    client_id, code, verifier = await complete_authorize(pg_engine, service)
    stale_user = await make_user(pg_engine, f"{secrets.token_hex(4)}@example.com")
    stale_hash = secrets.token_hex(32)
    async with pg_engine.begin() as conn:
        await conn.execute(
            insert(auth_tokens).values(
                token_hash=stale_hash,
                user_id=stale_user,
                kind="access",
                expires_at=datetime.now(UTC) - timedelta(days=1),
                family_id="stale-family",
            )
        )

    await service.exchange(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
        }
    )

    async with pg_engine.connect() as conn:
        row = (
            await conn.execute(
                select(auth_tokens.c.token_hash).where(auth_tokens.c.token_hash == stale_hash)
            )
        ).first()
    assert row is None, "an expired row must be purged by the next mint"


async def test_reusing_a_rotated_refresh_token_revokes_the_whole_family(pg_engine):
    """Rotation means a healthy client never replays an old token — seeing one means it
    leaked, so the entire family dies (RFC 9700 §4.14)."""
    from sqlalchemy import select

    from pse_edge_mcp.auth import hash_token
    from pse_edge_mcp.db import auth_tokens

    service = OAuthService(pg_engine)
    client_id, code, verifier = await complete_authorize(pg_engine, service)
    first = await service.exchange(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
        }
    )
    second = await service.exchange(
        {
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        }
    )

    # The attacker replays the token the legitimate client already rotated away.
    with pytest.raises(OAuthError, match="reuse detected"):
        await service.exchange(
            {
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": client_id,
            }
        )

    # The victim's current tokens are now dead too — that is the point.
    async with pg_engine.connect() as conn:
        row = (
            await conn.execute(
                select(auth_tokens.c.revoked_at).where(
                    auth_tokens.c.token_hash == hash_token(second["access_token"])
                )
            )
        ).first()
    assert row.revoked_at is not None, "the whole family must be revoked, not just the replay"

    with pytest.raises(OAuthError):
        await service.exchange(
            {
                "grant_type": "refresh_token",
                "refresh_token": second["refresh_token"],
                "client_id": client_id,
            }
        )


async def test_unsupported_grant_type_is_refused(pg_engine):
    with pytest.raises(OAuthError) as caught:
        await OAuthService(pg_engine).exchange({"grant_type": "password"})
    assert caught.value.error == "unsupported_grant_type"
