"""Passkey (WebAuthn) signup and login, plus the short-lived web sessions around them.

Plan §6: no passwords, ever. Email proves ownership of the identifier and is the recovery
path; a passkey is the credential. Multiple passkeys per account, so laptop and phone can
both be enrolled.

Session and challenge state lives in Postgres rather than in process memory, because HTTP
mode is stateless by default — the request that finishes a ceremony may land on a
different replica than the one that started it.

Security notes worth stating:

- The **challenge is stored server-side, per session**, and cleared once consumed, so a
  captured ceremony response cannot be replayed.
- Session ids are random and stored **hashed**, like bearer tokens: a database leak does
  not yield usable cookies.
- Enrolling requires a session already `verified` by email, and logging in produces a
  session that is only `authenticated` after the assertion verifies. A session's `kind` is
  the authority on what it may do next.
- `sign_count` is persisted and passed back to py_webauthn, which rejects a counter that
  fails to advance — the standard cloned-authenticator signal.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)

from .auth import hash_token
from .db import email_verifications, users, web_sessions, webauthn_credentials

# A cheap abuse brake (plan §6), not a security control: it raises the cost of bulk
# throwaway signups. Kept small and readable on purpose — a vendored 100k-domain list
# would imply a completeness this cannot have, and would rot silently.
DISPOSABLE_EMAIL_DOMAINS = frozenset(
    {
        "10minutemail.com",
        "guerrillamail.com",
        "mailinator.com",
        "tempmail.com",
        "temp-mail.org",
        "throwawaymail.com",
        "yopmail.com",
        "trashmail.com",
        "getnada.com",
        "sharklasers.com",
        "dispostable.com",
        "maildrop.cc",
        "fakeinbox.com",
        "mintemail.com",
    }
)

SESSION_TTL_MINUTES = 20
VERIFICATION_TTL_MINUTES = 30
SESSION_COOKIE = "pse_session"


class PasskeyError(Exception):
    """User-facing failure in a signup/login flow."""


@dataclass(frozen=True)
class WebSession:
    sid: str  # plaintext, for the cookie — never stored
    kind: str
    email: str | None
    user_id: str | None

    @property
    def csrf_token(self) -> str:
        """A per-session token for state-changing forms.

        Derived from the session id rather than stored, so it needs no column and cannot
        drift out of sync: knowing it requires already holding the cookie. SameSite=Lax
        already blocks the cross-site POST, so this is defence in depth against a
        same-site injection or a future SameSite relaxation.
        """
        return hash_token("csrf:" + self.sid)[:32]


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class PasskeyService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        public_url: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._origin = public_url.rstrip("/")
        # rp_id is the registrable domain: WebAuthn scopes credentials to it, and it must
        # not include scheme or port.
        self._rp_id = urlparse(self._origin).hostname or "localhost"
        self._now = now or (lambda: datetime.now(UTC))

    # --- sessions ------------------------------------------------------------

    async def create_session(
        self, kind: str, *, email: str | None = None, user_id: str | None = None
    ) -> WebSession:
        sid = secrets.token_urlsafe(32)
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(web_sessions).values(
                    sid_hash=hash_token(sid),
                    kind=kind,
                    email=email,
                    user_id=user_id,
                    expires_at=self._now() + timedelta(minutes=SESSION_TTL_MINUTES),
                )
            )
        return WebSession(sid=sid, kind=kind, email=email, user_id=user_id)

    async def load_session(self, sid: str | None) -> WebSession | None:
        if not sid:
            return None
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        web_sessions.c.kind,
                        web_sessions.c.email,
                        web_sessions.c.user_id,
                        web_sessions.c.expires_at,
                    ).where(web_sessions.c.sid_hash == hash_token(sid))
                )
            ).first()
        if row is None or row.expires_at <= self._now():
            return None
        return WebSession(sid=sid, kind=row.kind, email=row.email, user_id=row.user_id)

    async def _set_challenge(self, sid: str, challenge: bytes | None) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(web_sessions)
                .where(web_sessions.c.sid_hash == hash_token(sid))
                .values(current_challenge=_b64e(challenge) if challenge else None)
            )

    # --- email verification --------------------------------------------------

    async def start_signup(self, email: str) -> str:
        """Create a verification token for `email`; returns the plaintext for the link."""
        email = email.strip().lower()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise PasskeyError("that does not look like an email address")
        if email.rsplit("@", 1)[1] in DISPOSABLE_EMAIL_DOMAINS:
            raise PasskeyError(
                "that email provider is not accepted — please use a durable address, "
                "since it is also how you would recover access"
            )
        token = secrets.token_urlsafe(32)
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(email_verifications).values(
                    token_hash=hash_token(token),
                    email=email,
                    expires_at=self._now() + timedelta(minutes=VERIFICATION_TTL_MINUTES),
                )
            )
        return token

    async def consume_verification(self, token: str) -> WebSession:
        """Verify an email link and hand back a session allowed to enroll a passkey."""
        now = self._now()
        stmt = (
            update(email_verifications)
            .where(
                email_verifications.c.token_hash == hash_token(token),
                email_verifications.c.consumed_at.is_(None),
                email_verifications.c.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(email_verifications.c.email)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).first()
        if row is None:
            raise PasskeyError("this verification link is invalid, expired or already used")
        return await self.create_session("verified", email=row.email)

    # --- enrollment ----------------------------------------------------------

    async def begin_enrollment(self, session: WebSession) -> dict[str, Any]:
        if session.kind not in ("verified", "authenticated"):
            raise PasskeyError("verify your email before enrolling a passkey")
        email = session.email
        if not email:
            raise PasskeyError("session has no email")

        existing = await self._credentials_for_email(email)
        options = generate_registration_options(
            rp_id=self._rp_id,
            rp_name="PSE Edge MCP",
            user_id=email.encode(),
            user_name=email,
        )
        await self._set_challenge(session.sid, options.challenge)
        payload: dict[str, Any] = json.loads(options_to_json(options))
        # Tell the authenticator which credentials already exist, so a user cannot
        # silently enroll the same device twice.
        payload["excludeCredentials"] = [{"type": "public-key", "id": cid} for cid in existing]
        return payload

    async def finish_enrollment(self, session: WebSession, credential: dict[str, Any]) -> str:
        """Verify the attestation, creating the account on first enrollment.

        Returns the user id. The account is created here rather than at email
        verification, so an abandoned signup leaves no empty account behind.
        """
        if session.kind not in ("verified", "authenticated"):
            raise PasskeyError("verify your email before enrolling a passkey")
        email = session.email
        if not email:
            raise PasskeyError("session has no email")
        challenge = await self._pop_challenge(session.sid)

        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
            )
        except (InvalidRegistrationResponse, InvalidJSONStructure) as exc:
            raise PasskeyError(f"passkey enrollment failed: {exc}") from exc

        async with self._engine.begin() as conn:
            row = (await conn.execute(select(users.c.id).where(users.c.email == email))).first()
            if row is None:
                user_id = uuid.uuid4().hex
                await conn.execute(insert(users).values(id=user_id, email=email))
            else:
                user_id = row.id
            await conn.execute(
                insert(webauthn_credentials).values(
                    credential_id=_b64e(verified.credential_id),
                    user_id=user_id,
                    public_key=_b64e(verified.credential_public_key),
                    sign_count=verified.sign_count,
                    aaguid=str(verified.aaguid) if verified.aaguid else None,
                )
            )
            await conn.execute(
                update(web_sessions)
                .where(web_sessions.c.sid_hash == hash_token(session.sid))
                .values(kind="authenticated", user_id=user_id)
            )
        return user_id

    # --- login ---------------------------------------------------------------

    async def begin_login(self) -> tuple[WebSession, dict[str, Any]]:
        """Start a login ceremony.

        Deliberately does not take an email: with discoverable credentials the
        authenticator chooses, and not asking means the response cannot reveal whether an
        address is registered.
        """
        session = await self.create_session("pending")
        options = generate_authentication_options(rp_id=self._rp_id)
        await self._set_challenge(session.sid, options.challenge)
        return session, json.loads(options_to_json(options))

    async def finish_login(self, session: WebSession, credential: dict[str, Any]) -> str:
        """Verify an assertion; returns the user id and promotes the session."""
        challenge = await self._pop_challenge(session.sid)
        credential_id = credential.get("id") or ""

        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        webauthn_credentials.c.user_id,
                        webauthn_credentials.c.public_key,
                        webauthn_credentials.c.sign_count,
                        users.c.email,
                        users.c.disabled_at,
                    )
                    .select_from(
                        webauthn_credentials.join(
                            users, webauthn_credentials.c.user_id == users.c.id
                        )
                    )
                    .where(webauthn_credentials.c.credential_id == credential_id)
                )
            ).first()
        if row is None:
            raise PasskeyError("unknown passkey")
        if row.disabled_at is not None:
            raise PasskeyError("this account is disabled")

        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                credential_public_key=_b64d(row.public_key),
                credential_current_sign_count=row.sign_count,
            )
        except (InvalidAuthenticationResponse, InvalidJSONStructure) as exc:
            raise PasskeyError(f"passkey login failed: {exc}") from exc

        async with self._engine.begin() as conn:
            # Persisting the counter is what makes clone detection work across requests.
            await conn.execute(
                update(webauthn_credentials)
                .where(webauthn_credentials.c.credential_id == credential_id)
                .values(sign_count=verified.new_sign_count)
            )
            await conn.execute(
                update(web_sessions)
                .where(web_sessions.c.sid_hash == hash_token(session.sid))
                .values(kind="authenticated", user_id=row.user_id, email=row.email)
            )
        return str(row.user_id)

    # --- helpers -------------------------------------------------------------

    async def _pop_challenge(self, sid: str) -> bytes:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(web_sessions.c.current_challenge).where(
                        web_sessions.c.sid_hash == hash_token(sid)
                    )
                )
            ).first()
            if row is None or not row.current_challenge:
                raise PasskeyError("no ceremony in progress for this session")
            await conn.execute(
                update(web_sessions)
                .where(web_sessions.c.sid_hash == hash_token(sid))
                .values(current_challenge=None)
            )
        return _b64d(row.current_challenge)

    async def _credentials_for_email(self, email: str) -> list[str]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(webauthn_credentials.c.credential_id)
                    .select_from(
                        webauthn_credentials.join(
                            users, webauthn_credentials.c.user_id == users.c.id
                        )
                    )
                    .where(users.c.email == email)
                )
            ).all()
        return [row.credential_id for row in rows]

    async def list_credentials(self, user_id: str) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        webauthn_credentials.c.credential_id,
                        webauthn_credentials.c.aaguid,
                        webauthn_credentials.c.created_at,
                    ).where(webauthn_credentials.c.user_id == user_id)
                )
            ).all()
        return [dict(row._mapping) for row in rows]

    async def delete_credential(self, user_id: str, credential_id: str) -> bool:
        """Remove one passkey, refusing to remove the last (that would orphan the account)."""
        async with self._engine.begin() as conn:
            remaining = (
                await conn.execute(
                    select(webauthn_credentials.c.credential_id).where(
                        webauthn_credentials.c.user_id == user_id
                    )
                )
            ).all()
            if len(remaining) <= 1:
                raise PasskeyError(
                    "cannot remove your only passkey — enroll another one first, "
                    "otherwise you would lose access to the account"
                )
            result = await conn.execute(
                delete(webauthn_credentials).where(
                    webauthn_credentials.c.user_id == user_id,
                    webauthn_credentials.c.credential_id == credential_id,
                )
            )
        return bool(result.rowcount)
