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
from pse_edge_mcp.market_calendar import MarketCalendar
from pse_edge_mcp.oauth import OAuthService
from pse_edge_mcp.passkeys import PasskeyService
from pse_edge_mcp.server import build_server

pytestmark = pytest.mark.postgres

PUBLIC_URL = "http://localhost"  # rp_id 'localhost' is what the soft device signs for
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
        oauth=OAuthService(pg_engine),
        passkeys=PasskeyService(pg_engine, public_url=PUBLIC_URL),
        email=email,
        public_url=PUBLIC_URL,
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
        consent = await http.post("/consent", data={"flow_id": flow_id})
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
        assert len(called.json()["result"]["tools"]) == 11

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
        consent = await http.post("/consent", data={"flow_id": flow_id})
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
