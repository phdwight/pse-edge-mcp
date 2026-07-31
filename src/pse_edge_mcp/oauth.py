"""OAuth 2.1 authorization-server core: DCR, authorize, PKCE code exchange, refresh.

Implemented directly rather than on Authlib — a deliberate deviation from plan §6,
taken at the revisit point the plan itself scheduled: Authlib 1.7 ships server
integrations for Flask and Django only (verified empirically; `starlette_client` is the
*client* side), so using it here would mean adapting a Flask-shaped server to Starlette.
The surface we need is small and enumerable — public clients, authorization-code with
mandatory S256 PKCE, refresh rotation — and every rule below is pinned by a test.

The rules that matter, spelled out because getting any of them wrong is a vulnerability:

- **Never redirect to an unvalidated URI.** An unknown client_id or an unregistered
  redirect_uri is a fatal, non-redirecting error (`FatalAuthorizeError`) — redirecting
  would make us an open redirector. Only errors on a *validated* redirect target go back
  via the redirect (`RedirectAuthorizeError`).
- **Exact-match redirect URIs.** String equality against the registered list; no prefix
  or substring logic, per OAuth 2.1.
- **PKCE is mandatory, S256 only.** `plain` is rejected. Verifier length is enforced
  (43–128, RFC 7636 §4.1) and compared in constant time.
- **Codes are single-use, short-lived, and stored hashed.** Consumption is one atomic
  `UPDATE … WHERE consumed_at IS NULL RETURNING`, so two racing exchanges cannot both
  succeed even across replicas.
- **Refresh tokens rotate, and reuse is theft.** Every refresh mints a new pair and
  revokes the old token within its `family_id`. A revoked family member presented again
  means the token leaked — the whole family is revoked (RFC 9700 §4.14 guidance).
- **Public clients only, no secrets.** DCR issues no client_secret; there is nothing to
  store, leak, or verify. PKCE binds the code to the client instance instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from .auth import generate_token, hash_token
from .db import auth_tokens, oauth_clients, oauth_flows

FLOW_TTL_MINUTES = 15
CODE_TTL_SECONDS = 300
DEFAULT_SCOPE = "mcp"


class OAuthError(Exception):
    """Token/registration endpoint failure -> RFC 6749 JSON error."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def payload(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


class FatalAuthorizeError(Exception):
    """Authorize failure where redirecting is forbidden (bad client / redirect_uri)."""


class RedirectAuthorizeError(Exception):
    """Authorize failure on a *validated* redirect target; carries the error redirect."""

    def __init__(self, redirect_url: str) -> None:
        super().__init__(redirect_url)
        self.redirect_url = redirect_url


@dataclass(frozen=True)
class Flow:
    flow_id: str
    client_id: str
    client_name: str
    redirect_uri: str
    state: str | None
    scope: str


def _redirect_uri_acceptable(uri: str) -> bool:
    """https anywhere, plain http only on loopback (native/dev clients), no fragments."""
    parsed = urlparse(uri)
    if parsed.fragment or not parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")


def _pkce_matches(verifier: str, challenge: str) -> bool:
    if not (43 <= len(verifier) <= 128):  # RFC 7636 §4.1
        return False
    digest = hashlib.sha256(verifier.encode("ascii", errors="replace")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(expected, challenge)


class OAuthService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        access_ttl_min: int = 30,
        refresh_ttl_days: int = 30,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._access_ttl = timedelta(minutes=access_ttl_min)
        self._refresh_ttl = timedelta(days=refresh_ttl_days)
        self._now = now or (lambda: datetime.now(UTC))

    # --- RFC 7591 dynamic client registration --------------------------------

    async def register_client(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise OAuthError("invalid_client_metadata", "registration body must be a JSON object")
        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be a non-empty list")
        for uri in redirect_uris:
            if not isinstance(uri, str) or not _redirect_uri_acceptable(uri):
                raise OAuthError(
                    "invalid_redirect_uri",
                    f"redirect_uri not acceptable: {uri!r} (https, or http on loopback; "
                    "no fragments)",
                )
        client_name = payload.get("client_name") or "Unnamed MCP client"
        client_id = secrets.token_urlsafe(16)

        async with self._engine.begin() as conn:
            await conn.execute(
                insert(oauth_clients).values(
                    client_id=client_id,
                    client_name=str(client_name)[:200],
                    redirect_uris=redirect_uris,
                )
            )
        # Public client: no secret is issued, PKCE binds the code instead.
        return {
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }

    # --- authorize -----------------------------------------------------------

    async def begin_authorize(self, params: Mapping[str, str]) -> Flow:
        """Validate an authorization request and persist it as a flow.

        Order matters: client_id and redirect_uri are validated FIRST, and their
        failures never redirect. Everything after that has a trusted redirect target,
        so failures go back to the client via the redirect, per RFC 6749 §4.1.2.1.
        """
        client_id = params.get("client_id", "")
        async with self._engine.connect() as conn:
            client = (
                await conn.execute(
                    select(
                        oauth_clients.c.client_id,
                        oauth_clients.c.client_name,
                        oauth_clients.c.redirect_uris,
                    ).where(oauth_clients.c.client_id == client_id)
                )
            ).first()
        if client is None:
            raise FatalAuthorizeError("unknown client_id")

        redirect_uri = params.get("redirect_uri", "")
        if redirect_uri not in client.redirect_uris:  # exact match, never prefix
            raise FatalAuthorizeError("redirect_uri is not registered for this client")

        def bounce(error: str, description: str) -> RedirectAuthorizeError:
            query = {"error": error, "error_description": description}
            if params.get("state"):
                query["state"] = params["state"]
            separator = "&" if "?" in redirect_uri else "?"
            return RedirectAuthorizeError(f"{redirect_uri}{separator}{urlencode(query)}")

        if params.get("response_type") != "code":
            raise bounce("unsupported_response_type", "only response_type=code is supported")
        challenge = params.get("code_challenge", "")
        if not challenge:
            raise bounce("invalid_request", "code_challenge is required (PKCE is mandatory)")
        if params.get("code_challenge_method", "S256") != "S256":
            raise bounce("invalid_request", "only code_challenge_method=S256 is supported")

        flow_id = secrets.token_urlsafe(16)
        scope = params.get("scope") or DEFAULT_SCOPE
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(oauth_flows).values(
                    flow_id=flow_id,
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    state=params.get("state"),
                    code_challenge=challenge,
                    scope=scope,
                    expires_at=self._now() + timedelta(minutes=FLOW_TTL_MINUTES),
                )
            )
        return Flow(
            flow_id=flow_id,
            client_id=client_id,
            client_name=client.client_name,
            redirect_uri=redirect_uri,
            state=params.get("state"),
            scope=scope,
        )

    async def load_flow(self, flow_id: str) -> Flow | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        oauth_flows.c.flow_id,
                        oauth_flows.c.client_id,
                        oauth_flows.c.redirect_uri,
                        oauth_flows.c.state,
                        oauth_flows.c.scope,
                        oauth_flows.c.expires_at,
                        oauth_flows.c.code_hash,
                        oauth_clients.c.client_name,
                    )
                    .select_from(
                        oauth_flows.join(
                            oauth_clients, oauth_flows.c.client_id == oauth_clients.c.client_id
                        )
                    )
                    .where(oauth_flows.c.flow_id == flow_id)
                )
            ).first()
        if row is None or row.expires_at <= self._now() or row.code_hash is not None:
            return None  # unknown, stale, or already past the consent step
        return Flow(
            flow_id=row.flow_id,
            client_id=row.client_id,
            client_name=row.client_name,
            redirect_uri=row.redirect_uri,
            state=row.state,
            scope=row.scope or DEFAULT_SCOPE,
        )

    async def issue_code(self, flow_id: str, user_id: str) -> str:
        """Consent granted: bind the flow to the user, mint the code, build the redirect."""
        code = secrets.token_urlsafe(32)
        now = self._now()
        stmt = (
            update(oauth_flows)
            .where(
                oauth_flows.c.flow_id == flow_id,
                oauth_flows.c.code_hash.is_(None),  # consent can only happen once
                oauth_flows.c.expires_at > now,
            )
            .values(
                user_id=user_id,
                code_hash=hash_token(code),
                code_expires_at=now + timedelta(seconds=CODE_TTL_SECONDS),
            )
            .returning(oauth_flows.c.redirect_uri, oauth_flows.c.state)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            raise OAuthError("invalid_request", "authorization flow expired or already used")
        query = {"code": code}
        if row.state:
            query["state"] = row.state
        separator = "&" if "?" in row.redirect_uri else "?"
        return f"{row.redirect_uri}{separator}{urlencode(query)}"

    # --- token endpoint ------------------------------------------------------

    async def exchange(self, form: Mapping[str, str]) -> dict[str, Any]:
        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            return await self._exchange_code(form)
        if grant_type == "refresh_token":
            return await self._refresh(form)
        raise OAuthError("unsupported_grant_type", f"unsupported grant_type {grant_type!r}")

    async def _exchange_code(self, form: Mapping[str, str]) -> dict[str, Any]:
        code = form.get("code", "")
        verifier = form.get("code_verifier", "")
        if not code or not verifier:
            raise OAuthError("invalid_request", "code and code_verifier are required")

        now = self._now()
        # Atomic single-use: the first exchange consumes the row, any racing second
        # exchange sees consumed_at set and fails — across replicas too.
        stmt = (
            update(oauth_flows)
            .where(oauth_flows.c.code_hash == hash_token(code), oauth_flows.c.consumed_at.is_(None))
            .values(consumed_at=now)
            .returning(
                oauth_flows.c.client_id,
                oauth_flows.c.redirect_uri,
                oauth_flows.c.user_id,
                oauth_flows.c.scope,
                oauth_flows.c.code_challenge,
                oauth_flows.c.code_expires_at,
            )
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            raise OAuthError("invalid_grant", "authorization code is unknown or already used")
        if row.code_expires_at is None or row.code_expires_at <= now or row.user_id is None:
            raise OAuthError("invalid_grant", "authorization code expired")
        if form.get("client_id", "") != row.client_id:
            raise OAuthError("invalid_grant", "client_id does not match the authorization")
        if form.get("redirect_uri", "") != row.redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization")
        if not _pkce_matches(verifier, row.code_challenge):
            raise OAuthError("invalid_grant", "PKCE verification failed")

        return await self._mint(row.user_id, row.client_id, row.scope or DEFAULT_SCOPE)

    async def _refresh(self, form: Mapping[str, str]) -> dict[str, Any]:
        presented = form.get("refresh_token", "")
        if not presented:
            raise OAuthError("invalid_request", "refresh_token is required")
        presented_hash = hash_token(presented)
        now = self._now()

        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        auth_tokens.c.user_id,
                        auth_tokens.c.client_id,
                        auth_tokens.c.family_id,
                        auth_tokens.c.expires_at,
                        auth_tokens.c.revoked_at,
                    ).where(
                        auth_tokens.c.token_hash == presented_hash,
                        auth_tokens.c.kind == "refresh",
                    )
                )
            ).first()
        if row is None:
            raise OAuthError("invalid_grant", "unknown refresh token")
        if row.revoked_at is not None:
            # Rotation means a legitimate client never presents an old token; seeing one
            # again means it leaked. Kill the whole family (RFC 9700 §4.14).
            if row.family_id:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        update(auth_tokens)
                        .where(
                            auth_tokens.c.family_id == row.family_id,
                            auth_tokens.c.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                    )
            raise OAuthError("invalid_grant", "refresh token reuse detected; session revoked")
        if row.expires_at <= now:
            raise OAuthError("invalid_grant", "refresh token expired")
        if form.get("client_id", "") != (row.client_id or ""):
            raise OAuthError("invalid_grant", "client_id does not match this refresh token")

        # Rotate: retire the presented token, mint a successor in the same family.
        async with self._engine.begin() as conn:
            await conn.execute(
                update(auth_tokens)
                .where(auth_tokens.c.token_hash == presented_hash)
                .values(revoked_at=now)
            )
        return await self._mint(row.user_id, row.client_id, DEFAULT_SCOPE, row.family_id)

    async def _mint(
        self, user_id: str, client_id: str | None, scope: str, family_id: str | None = None
    ) -> dict[str, Any]:
        access, refresh = generate_token(), generate_token()
        family = family_id or uuid.uuid4().hex
        now = self._now()
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(auth_tokens).values(
                    [
                        {
                            "token_hash": hash_token(access),
                            "user_id": user_id,
                            "kind": "access",
                            "expires_at": now + self._access_ttl,
                            "client_id": client_id,
                            "family_id": family,
                        },
                        {
                            "token_hash": hash_token(refresh),
                            "user_id": user_id,
                            "kind": "refresh",
                            "expires_at": now + self._refresh_ttl,
                            "client_id": client_id,
                            "family_id": family,
                        },
                    ]
                )
            )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": int(self._access_ttl.total_seconds()),
            "refresh_token": refresh,
            "scope": scope,
        }
