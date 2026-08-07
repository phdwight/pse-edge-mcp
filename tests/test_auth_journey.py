"""The whole Phase 5 journey, driven through the real ASGI stack.

signup → verification email → passkey enrollment → dynamic client registration →
authorize → passkey login → consent → PKCE exchange → authenticated MCP call → refresh.

A `SoftWebauthnDevice` stands in for a browser authenticator, so the WebAuthn ceremonies
are exercised for real — real challenges, real signatures, real py_webauthn verification —
without a browser. Everything runs against a migrated Postgres and the actual AuthApp /
AuthMiddleware / MCP app composition that `__main__` builds.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp.server.transport_security import TransportSecuritySettings
from soft_webauthn import SoftWebauthnDevice

from pse_edge_mcp.auth import QuotaTracker, TokenService
from pse_edge_mcp.auth_app import AuthApp
from pse_edge_mcp.auth_middleware import AuthMiddleware
from pse_edge_mcp.auth_store import PostgresAuthStore
from pse_edge_mcp.config import Settings
from pse_edge_mcp.email import EmailSendError
from pse_edge_mcp.market_calendar import MarketCalendar
from pse_edge_mcp.oauth import OAuthService
from pse_edge_mcp.passkeys import PasskeyService
from pse_edge_mcp.server import build_server

pytestmark = pytest.mark.postgres

PUBLIC_URL = "http://localhost"  # rp_id 'localhost' is what the soft device signs for
ADMIN_EMAIL = "operator@example.com"  # in PSE_ADMIN_EMAILS for the fixture
REDIRECT = "http://127.0.0.1:33418/callback"
MNL = ZoneInfo("Asia/Manila")


class CapturingEmail:
    """Stands in for ZeptoMail; keeps the verification link so the test can 'click' it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to: str, subject: str, html: str) -> None:
        self.sent.append({"to": to, "subject": subject, "html": html})

    @property
    def last_link(self) -> str:
        html = self.sent[-1]["html"]
        start = html.index('href="') + len('href="')
        return html[start : html.index('"', start)]


class ClosedMarket(MarketCalendar):
    def now(self) -> datetime:
        return datetime(2026, 7, 30, 16, 30, tzinfo=MNL)


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.fixture
def stack(pg_engine):
    """The exact composition __main__ builds: AuthApp( AuthMiddleware( MCP app ) )."""
    settings = Settings(throttle_rate_per_sec=1000, public_url=PUBLIC_URL)
    mcp = build_server(settings, calendar=ClosedMarket())
    app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        # The DNS-rebinding guard checks the Host header; ASGITransport sends the one
        # from base_url, and rp_id must be a real domain ('localhost'), not an IP.
        transport_security=TransportSecuritySettings(
            allowed_hosts=["localhost", "127.0.0.1"],
            allowed_origins=[PUBLIC_URL],
        ),
    )
    guarded = AuthMiddleware(
        app,
        TokenService(PostgresAuthStore(pg_engine), cache_ttl_sec=0.0),
        QuotaTracker(),
        resource_metadata_url=f"{PUBLIC_URL}/.well-known/oauth-protected-resource",
    )
    email = CapturingEmail()
    surface = AuthApp(
        guarded,
        # `resource` mirrors what asgi.create_app passes. A fixture that leaves a setting
        # at a default production always sets is a blind spot, not a test — that is exactly
        # how the transport-security 421 survived a green suite.
        oauth=OAuthService(pg_engine, resource=f"{PUBLIC_URL}/mcp"),
        passkeys=PasskeyService(pg_engine, public_url=PUBLIC_URL),
        email=email,
        public_url=PUBLIC_URL,
        engine=pg_engine,
        # One known operator, so the machine-client panel can be tested on both sides:
        # this email sees it, any other does not.
        admin_emails=frozenset({ADMIN_EMAIL}),
    )
    return surface, email, app


@asynccontextmanager
async def serving(stack):
    """Start the app and hand back a client.

    The Starlette lifespan is entered *inside the test's own task*: httpx's ASGITransport
    does not run it (so the MCP session manager's task group would be uninitialised), and
    entering it in a fixture instead exits it from a different task, which anyio rejects
    with "Attempted to exit cancel scope in a different task".
    """
    surface, email, app = stack
    async with app.router.lifespan_context(app), client(surface) as http:
        yield http, email


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PUBLIC_URL, follow_redirects=False
    )


async def enroll_passkey(http: httpx.AsyncClient, email: CapturingEmail, address: str):
    """signup → email link → passkey enrollment. Returns the soft authenticator."""
    signup = await http.post("/signup", data={"email": address})
    assert signup.status_code == 200
    assert "check your email" in signup.text.lower()

    link = email.last_link
    verify = await http.get(urlparse(link).path, params=parse_qs(urlparse(link).query))
    assert verify.status_code == 302, "a valid link starts an enrollment session"

    options = (await http.post("/enroll/options", json={})).json()
    device = SoftWebauthnDevice()
    attestation = device.create(
        {
            "publicKey": {
                "rp": options["rp"],
                "user": {
                    "id": b64d(options["user"]["id"]),
                    "name": address,
                    "displayName": address,
                },
                "challenge": b64d(options["challenge"]),
                "pubKeyCredParams": options["pubKeyCredParams"],
                "attestation": "none",
            }
        },
        PUBLIC_URL,
    )
    finish = await http.post(
        "/enroll/finish",
        json={
            "id": attestation["id"].decode().rstrip("="),
            "rawId": b64e(attestation["rawId"]),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64e(attestation["response"]["clientDataJSON"]),
                "attestationObject": b64e(attestation["response"]["attestationObject"]),
            },
        },
    )
    assert finish.status_code == 200, finish.text
    return device


async def passkey_login(http: httpx.AsyncClient, device: SoftWebauthnDevice, flow_id: str) -> str:
    options = (await http.post("/login/options", json={})).json()
    assertion = device.get(
        {"publicKey": {"challenge": b64d(options["challenge"]), "rpId": "localhost"}},
        PUBLIC_URL,
    )
    response = await http.post(
        "/login/finish",
        json={
            "flow_id": flow_id,
            "credential": {
                "id": assertion["id"].decode().rstrip("="),
                "rawId": b64e(assertion["rawId"]),
                "type": "public-key",
                "response": {
                    "clientDataJSON": b64e(assertion["response"]["clientDataJSON"]),
                    "authenticatorData": b64e(assertion["response"]["authenticatorData"]),
                    "signature": b64e(assertion["response"]["signature"]),
                    "userHandle": b64e(assertion["response"]["userHandle"] or b""),
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["next"]


def flow_id_from_consent(html: str) -> str:
    marker = "name=flow_id value='"
    start = html.index(marker) + len(marker)
    return html[start : html.index("'", start)]


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = b64e(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


# --- the journey -------------------------------------------------------------


async def test_signup_enroll_authorize_and_call_mcp_with_the_issued_token(stack):
    """The whole point of Phase 5, end to end: a stranger becomes an authenticated MCP
    caller without a password ever existing."""
    async with serving(stack) as (http, email):
        # 1. Unauthenticated MCP access is refused, and the refusal says where to go.
        blocked = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert blocked.status_code == 401
        assert "resource_metadata" in blocked.headers["www-authenticate"]

        # 2. Discovery, as an MCP client would perform it.
        resource = (await http.get("/.well-known/oauth-protected-resource")).json()
        assert resource["authorization_servers"] == [PUBLIC_URL]
        metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
        assert metadata["code_challenge_methods_supported"] == ["S256"]

        # 3. Signup and passkey enrollment.
        device = await enroll_passkey(http, email, "journey@example.com")
        assert email.sent[0]["to"] == "journey@example.com"

        # 4. The client registers itself (RFC 7591) — no operator involvement.
        registration = await http.post(
            "/oauth/register",
            json={"client_name": "Claude", "redirect_uris": [REDIRECT]},
        )
        assert registration.status_code == 201
        client_id = registration.json()["client_id"]

        # 5. Simulate a fresh browser: enrollment left this session authenticated, and
        # we want the login ceremony exercised too, not skipped.
        http.cookies.clear()

        verifier, challenge = pkce_pair()
        authorize = await http.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "opaque-state",
            },
        )
        assert authorize.status_code == 302, "no authenticated session yet -> login"
        flow_id = parse_qs(urlparse(authorize.headers["location"]).query)["flow"][0]

        # 6. Passkey login, then back to authorize, which now shows consent.
        next_url = await passkey_login(http, device, flow_id)
        consent_page = await http.get(urlparse(next_url).path, params={"flow": flow_id})
        assert consent_page.status_code == 200
        assert "Claude" in consent_page.text

        # 7. Consent issues the code on the registered redirect, with state intact.
        # The CSRF token comes from the rendered page, exactly as a browser would submit it.
        csrf = consent_page.text.split("name=csrf_token value='")[1].split("'")[0]
        consent = await http.post("/consent", data={"flow_id": flow_id, "csrf_token": csrf})
        assert consent.status_code == 302
        location = urlparse(consent.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == REDIRECT
        query = parse_qs(location.query)
        assert query["state"] == ["opaque-state"]
        code = query["code"][0]

        # 8. PKCE exchange.
        token_response = await http.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": REDIRECT,
            },
        )
        assert token_response.status_code == 200
        assert token_response.headers["cache-control"] == "no-store"
        tokens = token_response.json()

        # 9. The token actually works against the MCP endpoint.
        called = await http.post(
            "/mcp",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert called.status_code == 200
        assert len(called.json()["result"]["tools"]) == 13

        # 10. Refresh rotates, and the new access token also works.
        refreshed = (
            await http.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": client_id,
                },
            )
        ).json()
        assert refreshed["access_token"] != tokens["access_token"]
        again = await http.post(
            "/mcp",
            headers={"Authorization": f"Bearer {refreshed['access_token']}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert again.status_code == 200


async def test_refresh_tokens_cannot_be_used_as_bearer_tokens(stack):
    """They share a table with access tokens; only kind='access' may authenticate."""
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "refresh-abuse@example.com")
        client_id = (await http.post("/oauth/register", json={"redirect_uris": [REDIRECT]})).json()[
            "client_id"
        ]
        verifier, challenge = pkce_pair()
        authorize = await http.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        # Enrollment already authenticated this session, so authorize renders consent
        # directly rather than bouncing through login.
        assert authorize.status_code == 200, authorize.text
        flow_id = flow_id_from_consent(authorize.text)
        csrf = authorize.text.split("name=csrf_token value='")[1].split("'")[0]
        consent = await http.post("/consent", data={"flow_id": flow_id, "csrf_token": csrf})
        code = parse_qs(urlparse(consent.headers["location"]).query)["code"][0]
        tokens = (
            await http.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "client_id": client_id,
                    "redirect_uri": REDIRECT,
                },
            )
        ).json()

        refused = await http.post(
            "/mcp",
            headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert refused.status_code == 401


async def test_a_replayed_verification_link_is_refused(stack):
    """Single-use: a link forwarded or found in a mailbox later must not enroll again."""
    async with serving(stack) as (http, email):
        await http.post("/signup", data={"email": "replay@example.com"})
        link = urlparse(email.last_link)
        first = await http.get(link.path, params=parse_qs(link.query))
        assert first.status_code == 302
        second = await http.get(link.path, params=parse_qs(link.query))
        assert second.status_code == 400
        assert "already used" in second.text


async def test_signup_does_not_reveal_whether_an_address_is_registered(stack):
    """The response must be identical for a new and an existing address, or the page
    becomes an account-enumeration oracle."""
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "known@example.com")
        existing = await http.post("/signup", data={"email": "known@example.com"})
        fresh = await http.post("/signup", data={"email": "stranger@example.com"})

    assert existing.status_code == fresh.status_code == 200
    assert existing.text == fresh.text


async def test_enrollment_requires_a_verified_session(stack):
    """No session, no passkey — otherwise anyone could bind a credential to any account."""
    async with serving(stack) as (http, _):
        response = await http.post("/enroll/options", json={})
    assert response.status_code == 400
    assert "session" in response.json()["error"]


async def test_a_tampered_passkey_assertion_is_refused(stack):
    """Flip a signature bit: py_webauthn must reject it."""
    async with serving(stack) as (http, email):
        device = await enroll_passkey(http, email, "tamper@example.com")
        options = (await http.post("/login/options", json={})).json()
        assertion = device.get(
            {"publicKey": {"challenge": b64d(options["challenge"]), "rpId": "localhost"}},
            PUBLIC_URL,
        )
        broken = bytearray(assertion["response"]["signature"])
        broken[-1] ^= 0x01
        response = await http.post(
            "/login/finish",
            json={
                "flow_id": "",
                "credential": {
                    "id": assertion["id"].decode().rstrip("="),
                    "rawId": b64e(assertion["rawId"]),
                    "type": "public-key",
                    "response": {
                        "clientDataJSON": b64e(assertion["response"]["clientDataJSON"]),
                        "authenticatorData": b64e(assertion["response"]["authenticatorData"]),
                        "signature": b64e(bytes(broken)),
                        "userHandle": b64e(assertion["response"]["userHandle"] or b""),
                    },
                },
            },
        )
    assert response.status_code == 400
    assert "failed" in response.json()["error"]


async def test_a_challenge_cannot_be_replayed(stack):
    """Challenges are single-use: the same assertion presented twice must fail the second
    time, even though it was valid the first."""
    async with serving(stack) as (http, email):
        device = await enroll_passkey(http, email, "challenge@example.com")
        options = (await http.post("/login/options", json={})).json()
        assertion = device.get(
            {"publicKey": {"challenge": b64d(options["challenge"]), "rpId": "localhost"}},
            PUBLIC_URL,
        )
        payload = {
            "flow_id": "",
            "credential": {
                "id": assertion["id"].decode().rstrip("="),
                "rawId": b64e(assertion["rawId"]),
                "type": "public-key",
                "response": {
                    "clientDataJSON": b64e(assertion["response"]["clientDataJSON"]),
                    "authenticatorData": b64e(assertion["response"]["authenticatorData"]),
                    "signature": b64e(assertion["response"]["signature"]),
                    "userHandle": b64e(assertion["response"]["userHandle"] or b""),
                },
            },
        }
        first = await http.post("/login/finish", json=payload)
        second = await http.post("/login/finish", json=payload)

    assert first.status_code == 200
    assert second.status_code == 400
    assert "no ceremony in progress" in second.json()["error"]


async def test_well_known_and_oauth_routes_are_reachable_without_a_token(stack):
    """You cannot present a token before you have one — the surface that issues them must
    sit outside the guard."""
    async with serving(stack) as (http, _):
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        ):
            assert (await http.get(path)).status_code == 200
        assert (await http.get("/signup")).status_code == 200
        assert (await http.get("/login")).status_code == 200
        # ...while the MCP endpoint itself stays guarded.
        assert (await http.post("/mcp", json={})).status_code == 401


# --- privacy surface (plan §6a) ----------------------------------------------


async def test_privacy_page_is_public_and_states_the_commitments(stack):
    """A policy nobody can read before signing up is not a policy — so it needs no auth,
    and it has to actually say the things §6a requires."""
    async with serving(stack) as (http, _):
        response = await http.get("/privacy")

    assert response.status_code == 200
    body = response.text.lower()
    assert "90 days" in body, "retention window must be stated"
    assert "delete your account" in body, "self-deletion right must be stated"
    assert "breach" in body, "breach-notification contact must be stated"
    assert "zeptomail" in body, "third-party processor must be disclosed"


async def test_disposable_email_domains_are_refused_at_signup(stack):
    """A cheap abuse brake (plan §6). Also honest: the address is the recovery path."""
    async with serving(stack) as (http, email):
        blocked = await http.post("/signup", data={"email": "burner@mailinator.com"})
        allowed = await http.post("/signup", data={"email": "real@example.com"})

    assert blocked.status_code == 400
    assert "not accepted" in blocked.text
    assert allowed.status_code == 200
    assert [m["to"] for m in email.sent] == ["real@example.com"], "no email for the blocked one"


async def test_account_page_shows_the_subject_their_own_data(stack):
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "mydata@example.com")
        page = await http.get("/account")

    assert page.status_code == 200
    assert "mydata@example.com" in page.text
    assert "1 passkey" in page.text


async def test_account_page_requires_a_session(stack):
    async with serving(stack) as (http, _):
        response = await http.get("/account")
    assert response.status_code == 302
    assert response.headers["location"].endswith("/login")


async def test_self_deletion_erases_the_account_through_the_endpoint(stack):
    """The §6a right, end to end: no request, no waiting period, no email exchange."""
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "goodbye@example.com")
        page = await http.get("/account")
        csrf = page.text.split("name=csrf_token value='")[1].split("'")[0]

        deleted = await http.post("/account/delete", data={"csrf_token": csrf})
        assert deleted.status_code == 200
        assert "erased" in deleted.text.lower()

        # The session cookie is cleared, so the account page sends them to login rather
        # than to a confusing "session expired".
        after = await http.get("/account")
        assert after.status_code == 302

        # Signing up again with the same address must be possible — erasure was complete.
        again = await http.post("/signup", data={"email": "goodbye@example.com"})
        assert again.status_code == 200


async def _authorize_tokens(
    http: httpx.AsyncClient, client_name: str = "Claude"
) -> tuple[str, dict[str, Any]]:
    """Register a DCR client and run authorize → consent → exchange, reusing the
    already-authenticated browser session. Returns (client_id, token response)."""
    registration = await http.post(
        "/oauth/register", json={"client_name": client_name, "redirect_uris": [REDIRECT]}
    )
    client_id: str = registration.json()["client_id"]
    verifier, challenge = pkce_pair()
    consent_page = await http.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
        },
    )
    assert consent_page.status_code == 200, "an authenticated session goes straight to consent"
    flow_id = flow_id_from_consent(consent_page.text)
    csrf = consent_page.text.split("name=csrf_token value='")[1].split("'")[0]
    consent = await http.post("/consent", data={"flow_id": flow_id, "csrf_token": csrf})
    code = parse_qs(urlparse(consent.headers["location"]).query)["code"][0]
    tokens = await http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
        },
    )
    assert tokens.status_code == 200
    return client_id, tokens.json()


async def test_the_security_tab_lists_sessions_and_revokes_one(stack):
    """The Sessions & tokens control: one row per connected client, named, and revoking
    it kills the whole token family — the client's refresh grant dies with it."""
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "sessions@example.com")
        client_id, tokens = await _authorize_tokens(http, client_name="Claude Desktop")

        page = await http.get("/account")
        assert "Claude Desktop" in page.text, "the connected client is listed by name"
        family = page.text.split("name=family_id value='")[1].split("'")[0]
        csrf = page.text.split("name=csrf_token value='")[1].split("'")[0]

        revoked = await http.post(
            "/account/sessions/revoke", data={"csrf_token": csrf, "family_id": family}
        )
        assert revoked.status_code == 302

        after = await http.get("/account")
        assert "Claude Desktop" not in after.text, "the revoked session drops off the page"

        refused = await http.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": client_id,
            },
        )
        assert refused.status_code == 400
        assert refused.json()["error"] == "invalid_grant", "the family is genuinely dead"


async def test_session_revoke_needs_a_valid_csrf_token(stack):
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "sessions-csrf@example.com")
        _, _ = await _authorize_tokens(http, client_name="Claude Desktop")
        page = await http.get("/account")
        family = page.text.split("name=family_id value='")[1].split("'")[0]

        forged = await http.post(
            "/account/sessions/revoke", data={"csrf_token": "nope", "family_id": family}
        )
        assert forged.status_code == 403
        after = await http.get("/account")
        assert "Claude Desktop" in after.text, "the session survives a forged request"


async def test_deletion_without_a_csrf_token_is_refused(stack):
    """SameSite=Lax already blocks the cross-site POST; this is the second lock."""
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "csrf@example.com")
        refused = await http.post("/account/delete", data={"csrf_token": "wrong"})
        assert refused.status_code == 403
        # ...and the account still exists.
        assert (await http.get("/account")).status_code == 200


async def test_consent_without_a_csrf_token_is_refused(stack):
    """An authorization granted by a forged POST would hand an attacker a real token."""
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "consent-csrf@example.com")
        client_id = (await http.post("/oauth/register", json={"redirect_uris": [REDIRECT]})).json()[
            "client_id"
        ]
        _, challenge = pkce_pair()
        authorize = await http.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        flow_id = flow_id_from_consent(authorize.text)

        forged = await http.post("/consent", data={"flow_id": flow_id, "csrf_token": "nope"})
        assert forged.status_code == 403

        # The genuine token, taken from the page the user actually saw, still works.
        csrf = authorize.text.split("name=csrf_token value='")[1].split("'")[0]
        granted = await http.post("/consent", data={"flow_id": flow_id, "csrf_token": csrf})
        assert granted.status_code == 302


@pytest.mark.postgres
async def test_a_failing_mail_provider_is_503_not_a_stack_trace(stack):
    """Seen in production: ZeptoMail answered 500, the exception escaped the handler, and
    the user typing their address got a bare "Internal Server Error".

    A provider having a bad day is an operational problem, not this user's bug to read a
    traceback about — and 500 invites them to conclude the address was the problem and try
    a different one, which cannot help.
    """
    surface, email, app = stack

    class BrokenEmail:
        async def send(self, **kwargs: Any) -> None:
            raise EmailSendError("ZeptoMail rejected the send (500) from='x@y': <empty body>")

    surface._email = BrokenEmail()  # type: ignore[assignment]
    async with serving(stack) as (http, _):
        response = await http.post("/signup", data={"email": "mailer-down@example.com"})

    assert response.status_code == 503, "retryable outage, not a bug and not a client error"
    assert "try again" in response.text.lower()
    assert "traceback" not in response.text.lower()
    assert "zeptomail" not in response.text.lower(), "provider names are for the log, not users"


@pytest.mark.postgres
async def test_the_front_door_is_a_page_not_a_bearer_token_error(stack):
    """Reported from production: signing in without an OAuth flow landed on `/`, which was
    not a route, so it fell through to the MCP app and told a freshly authenticated user
    "Missing bearer token" — asking them for something they have no way to obtain."""
    async with serving(stack) as (http, _):
        root = await http.get("/")
        favicon = await http.get("/favicon.ico")

    assert root.status_code == 200
    assert "bearer" not in root.text.lower()
    for link in ("/signup", "/login", "/privacy"):
        assert link in root.text, f"a visitor needs a way to reach {link}"
    assert favicon.status_code == 204, "browsers always ask; 401s here are log noise"


@pytest.mark.postgres
async def test_signing_in_without_a_flow_lands_on_the_account_page(stack):
    """The direct-browser path: no MCP client involved, so there is no authorize step to
    resume and the user should end up somewhere built for a person."""
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "front-door@example.com")
        root = await http.get("/")

    assert root.status_code == 302, "an authenticated visitor at / goes to their account"
    assert root.headers["location"].endswith("/account")


# --- client_credentials: headless machine-to-machine -------------------------
#
# The security property under test is one sentence: /oauth/register is open to the
# internet, so being able to register MUST NOT be enough to mint a token. Everything else
# here is ordinary OAuth plumbing; that one is the reason this grant needed care.


async def provision_machine_client(pg_engine, name: str = "langgraph-app") -> dict[str, str]:
    from pse_edge_mcp.admin import create_machine_client

    return await create_machine_client(pg_engine, name)


@pytest.mark.postgres
async def test_machine_client_mints_a_token_and_calls_mcp(stack, pg_engine):
    """The whole headless path: no browser, no passkey, no consent — just a secret."""
    machine = await provision_machine_client(pg_engine)

    async with serving(stack) as (http, _):
        minted = await http.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": machine["client_id"],
                "client_secret": machine["client_secret"],
                "scope": "mcp",
            },
        )
        assert minted.status_code == 200, minted.text
        body = minted.json()
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == 3600
        assert body["scope"] == "mcp"
        assert minted.headers["cache-control"] == "no-store"

        auth = {"Authorization": f"Bearer {body['access_token']}"}
        initialized = await http.post(
            "/mcp",
            headers=auth,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "langgraph", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["serverInfo"]["name"] == "pse-edge"

        called = await http.post(
            "/mcp",
            headers=auth,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "validate_symbol", "arguments": {"symbol": "SM"}},
            },
        )
    assert called.status_code == 200, called.text
    assert "error" not in called.json(), called.text


@pytest.mark.postgres
async def test_machine_client_authenticates_with_http_basic(stack, pg_engine):
    """RFC 6749 §2.3.1 names Basic as the method a server MUST accept; most SDKs default
    to it, while curl examples post the fields. Both have to work."""
    machine = await provision_machine_client(pg_engine, "basic-auth-app")
    credential = base64.b64encode(
        f"{machine['client_id']}:{machine['client_secret']}".encode()
    ).decode()

    async with serving(stack) as (http, _):
        minted = await http.post(
            "/oauth/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {credential}"},
        )

    assert minted.status_code == 200, minted.text
    assert minted.json()["access_token"].startswith("pse_")


@pytest.mark.postgres
async def test_a_dcr_registered_client_cannot_use_client_credentials(stack, pg_engine):
    """THE test. /oauth/register is open by design, so if registering were enough to use
    this grant, anyone on the internet could mint tokens for themselves.

    The client even self-declares the grant and sends a secret — neither is consulted.
    Authorization comes from `client_type`, which only the admin CLI writes.
    """
    async with serving(stack) as (http, _):
        registered = (
            await http.post(
                "/oauth/register",
                json={
                    "redirect_uris": [REDIRECT],
                    "client_name": "impostor",
                    # Self-declared and deliberately ignored by the server.
                    "grant_types": ["client_credentials", "authorization_code"],
                    "token_endpoint_auth_method": "client_secret_post",
                },
            )
        ).json()

        attempt = await http.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": registered["client_id"],
                "client_secret": "anything-at-all",
                "scope": "mcp",
            },
        )

    assert attempt.status_code == 400
    assert attempt.json()["error"] == "unauthorized_client"
    assert "access_token" not in attempt.json()


@pytest.mark.postgres
async def test_wrong_secret_and_unknown_client_are_both_invalid_client(stack, pg_engine):
    """Answered identically on purpose: distinguishing them turns an offline guess into
    an online oracle for which client ids exist."""
    machine = await provision_machine_client(pg_engine, "wrong-secret-app")

    async with serving(stack) as (http, _):
        wrong = await http.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": machine["client_id"],
                "client_secret": "not-the-secret",
            },
        )
        unknown = await http.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "mcp-does-not-exist",
                "client_secret": "whatever",
            },
        )

    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_client"
        # RFC 6749 §5.2: a 401 here must say how to authenticate.
        assert "www-authenticate" in response.headers
    assert wrong.json() == unknown.json(), "the two must be indistinguishable"


@pytest.mark.postgres
async def test_client_credentials_issues_no_refresh_token(stack, pg_engine):
    """The client already holds a long-lived secret it can present again. A refresh token
    would be a second credential of equal power without the rotation benefit."""
    machine = await provision_machine_client(pg_engine, "no-refresh-app")

    async with serving(stack) as (http, _):
        minted = await http.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": machine["client_id"],
                "client_secret": machine["client_secret"],
            },
        )

    assert minted.status_code == 200
    assert "refresh_token" not in minted.json()


@pytest.mark.postgres
async def test_revoking_a_machine_client_kills_its_outstanding_tokens(stack, pg_engine):
    """Clearing the secret alone would leave an up-to-an-hour window in which an already
    issued bearer still works."""
    from pse_edge_mcp.admin import revoke_machine_client

    machine = await provision_machine_client(pg_engine, "revoke-me-app")

    async with serving(stack) as (http, _):
        token = (
            await http.post(
                "/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": machine["client_id"],
                    "client_secret": machine["client_secret"],
                },
            )
        ).json()["access_token"]

        await revoke_machine_client(pg_engine, machine["client_id"])

        reused = await http.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        reminted = await http.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": machine["client_id"],
                "client_secret": machine["client_secret"],
            },
        )

    assert reused.status_code == 401, "an issued token must stop working immediately"
    assert reminted.status_code == 401


@pytest.mark.postgres
async def test_unsupported_scope_and_wrong_resource_are_refused(stack, pg_engine):
    """Silently narrowing a scope is how a client comes to believe it holds a permission
    it does not have, which surfaces much later as a confusing failure."""
    machine = await provision_machine_client(pg_engine, "scope-app")
    creds = {
        "grant_type": "client_credentials",
        "client_id": machine["client_id"],
        "client_secret": machine["client_secret"],
    }

    async with serving(stack) as (http, _):
        bad_scope = await http.post("/oauth/token", data={**creds, "scope": "mcp admin"})
        bad_resource = await http.post(
            "/oauth/token", data={**creds, "resource": "https://elsewhere.example/mcp"}
        )
        # The canonical resource, with and without a trailing slash, both work.
        good = await http.post("/oauth/token", data={**creds, "resource": f"{PUBLIC_URL}/mcp/"})

    assert bad_scope.status_code == 400
    assert bad_scope.json()["error"] == "invalid_scope"
    assert bad_resource.json()["error"] == "invalid_target"
    assert good.status_code == 200, good.text


@pytest.mark.postgres
async def test_unsupported_grant_type_is_rejected(stack):
    async with serving(stack) as (http, _):
        response = await http.post("/oauth/token", data={"grant_type": "password"})
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"


@pytest.mark.postgres
async def test_the_token_endpoint_is_rate_limited(stack, pg_engine):
    """The one endpoint where a long-lived credential can be guessed online."""
    machine = await provision_machine_client(pg_engine, "flood-app")
    attempt = {
        "grant_type": "client_credentials",
        "client_id": machine["client_id"],
        "client_secret": "wrong",
    }

    async with serving(stack) as (http, _):
        statuses = [(await http.post("/oauth/token", data=attempt)).status_code for _ in range(25)]
        limited = await http.post("/oauth/token", data=attempt)

    assert 429 in statuses, "guessing must be throttled, not merely refused forever"
    assert limited.status_code == 429
    assert limited.json()["error"] == "slow_down"
    assert int(limited.headers["retry-after"]) > 0


@pytest.mark.postgres
async def test_metadata_advertises_client_credentials_and_secret_auth(stack):
    async with serving(stack) as (http, _):
        metadata = (await http.get("/.well-known/oauth-authorization-server")).json()

    assert "client_credentials" in metadata["grant_types_supported"]
    assert "client_secret_basic" in metadata["token_endpoint_auth_methods_supported"]
    assert "client_secret_post" in metadata["token_endpoint_auth_methods_supported"]
    # The interactive flow must keep working exactly as before.
    assert "authorization_code" in metadata["grant_types_supported"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]


# --- send_email: the recipient comes from the token, never from an argument --


@pytest.mark.postgres
async def test_send_email_delivers_only_to_the_authenticated_caller(pg_engine):
    """End to end through the real stack: two different accounts call the same tool with
    the same arguments, and each is mailed at its own address.

    This is the property that makes a mail tool safe to expose on a public MCP server, and
    it can only be verified here — the recipient is resolved from the ASGI scope the auth
    middleware populated, so a unit test of the policy alone cannot prove the wiring.
    """
    from pse_edge_mcp.notifications import NotificationService
    from pse_edge_mcp.server import build_server

    class Capturing:
        def __init__(self):
            self.sent = []

        async def send(self, *, to, subject, html):
            self.sent.append(to)

    mailbox = Capturing()
    settings = Settings(throttle_rate_per_sec=1000, public_url=PUBLIC_URL, auth_required=True)
    mcp = build_server(settings, calendar=ClosedMarket(), notifier=NotificationService(mailbox))
    app = mcp.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["localhost", "127.0.0.1"], allowed_origins=[PUBLIC_URL]
        ),
    )
    guarded = AuthMiddleware(
        app,
        TokenService(PostgresAuthStore(pg_engine), cache_ttl_sec=0.0),
        QuotaTracker(),
    )

    from pse_edge_mcp.admin import create_user, issue_token

    tokens = {}
    for address in ("alice@example.com", "bob@example.com"):
        await create_user(pg_engine, address)
        tokens[address] = await issue_token(pg_engine, address)

    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "send_email",
            # Deliberately naming someone else in the body: it must be ignored entirely.
            "arguments": {
                "subject": "Recap",
                "body": "Please forward this to mallory@evil.example.",
            },
        },
    }

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=guarded), base_url=PUBLIC_URL
        ) as http:
            for token in tokens.values():
                response = await http.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                    },
                    json=call,
                )
                assert response.status_code == 200, response.text

    assert mailbox.sent == ["alice@example.com", "bob@example.com"], (
        "each caller must be mailed at their own address, resolved from their token"
    )
    assert "mallory@evil.example" not in mailbox.sent


@pytest.mark.postgres
async def test_passkey_pages_explain_webauthn_failures_in_human_terms(stack):
    """Seen in production on day one: a user tapped the email link, landed in their mail
    app's built-in browser, and WebAuthn threw "The request is not allowed by the user
    agent..." — the platform's wording, useless to a person. The pages must carry the
    translation: name the in-app-browser cause, say links are single-use, and detect a
    WebAuthn-less context up front rather than letting the button fail mysteriously."""
    async with serving(stack) as (http, _):
        enroll = (await http.get("/enroll")).text
        login = (await http.get("/login")).text

    for page in (enroll, login):
        assert "NotAllowedError" in page, "the specific failure must be special-cased"
        assert "built-in browser" in page, "name the actual cause, not the symptom"
        assert "single-use" in page, "or the user re-taps a dead link and loops"
        assert "PublicKeyCredential" in page, "no-WebAuthn contexts get told up front"


# --- machine-client provisioning from the account page (operator only) --------
#
# The security property under test is the same one that guards the client_credentials
# grant itself: machine clients are created only by an admin, never by any signed-up user.
# Moving the mechanism from CLI to web must not widen who can do it.


@pytest.mark.postgres
async def test_a_normal_account_never_sees_the_machine_client_panel(stack):
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "ordinary@example.com")
        page = await http.get("/account")

    assert page.status_code == 200
    assert "Machine clients" not in page.text, "a non-operator must not even see the surface"


@pytest.mark.postgres
async def test_a_normal_account_cannot_reach_the_machine_client_routes(stack):
    """Not just hidden — refused. And refused as 404, giving no hint the route exists."""
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, "sneaky@example.com")
        page = await http.get("/account")
        csrf = page.text.split("name=csrf_token value='")[1].split("'")[0]

        created = await http.post(
            "/account/machine-clients", data={"csrf_token": csrf, "name": "mine"}
        )
        revoked = await http.post(
            "/account/machine-clients/revoke",
            data={"csrf_token": csrf, "client_id": "mcp-anything"},
        )

    assert created.status_code == 404
    assert revoked.status_code == 404


def _created_field(page_text: str, name: str) -> str:
    """Pull client_id / client_secret out of the created-machine-client page."""
    return page_text.split(f"{name}</span>")[1].split("<code>")[1].split("</code>")[0]


@pytest.mark.postgres
async def test_an_operator_creates_a_machine_client_and_sees_the_secret_once(stack, pg_engine):
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, ADMIN_EMAIL)
        page = await http.get("/account")
        assert "Machine clients" in page.text, "the operator sees the panel"
        csrf = page.text.split("name=csrf_token value='")[1].split("'")[0]

        created = await http.post(
            "/account/machine-clients", data={"csrf_token": csrf, "name": "langgraph-app"}
        )
        assert created.status_code == 200
        assert "client_secret" in created.text
        assert created.headers["cache-control"] == "no-store", (
            "a cached copy of this page IS the credential"
        )
        secret = _created_field(created.text, "client_secret")
        client_id = _created_field(created.text, "client_id")
        assert secret and client_id.startswith("mcp-")

        # The secret is shown once and never rendered again — the account page must not leak it.
        back = await http.get("/account")
        assert secret not in back.text
        assert client_id in back.text, "but the client is listed so it can be revoked"

    # And the minted secret actually authenticates the client_credentials grant.
    minted = await _mint_via(pg_engine, client_id, secret)
    assert minted is not None


@pytest.mark.postgres
async def test_an_operator_revokes_a_machine_client_from_the_page(stack, pg_engine):
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, ADMIN_EMAIL)
        page = await http.get("/account")
        csrf = page.text.split("name=csrf_token value='")[1].split("'")[0]
        created = await http.post(
            "/account/machine-clients", data={"csrf_token": csrf, "name": "revoke-me"}
        )
        client_id = _created_field(created.text, "client_id")

        revoked = await http.post(
            "/account/machine-clients/revoke",
            data={"csrf_token": csrf, "client_id": client_id},
        )
        assert revoked.status_code == 302

        after = await http.get("/account")
        assert client_id not in after.text, "a revoked client drops off the list"


@pytest.mark.postgres
async def test_machine_client_creation_needs_a_valid_csrf_token(stack):
    async with serving(stack) as (http, email):
        await enroll_passkey(http, email, ADMIN_EMAIL)
        forged = await http.post(
            "/account/machine-clients", data={"csrf_token": "nope", "name": "x"}
        )
    assert forged.status_code == 403


async def _mint_via(pg_engine, client_id: str, secret: str):
    from pse_edge_mcp.oauth import OAuthService

    try:
        return await OAuthService(pg_engine).exchange(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
            }
        )
    except Exception:
        return None
