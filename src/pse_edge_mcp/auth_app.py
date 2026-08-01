"""The browser- and client-facing auth surface, as a plain ASGI app.

Routes (all outside `/mcp`, and all exempt from the bearer middleware):

  GET  /.well-known/oauth-protected-resource   RFC 9728 — points clients at the AS
  GET  /.well-known/oauth-authorization-server RFC 8414 — endpoint discovery
  POST /oauth/register                         RFC 7591 dynamic client registration
  GET  /oauth/authorize                        starts a flow, renders login/consent
  POST /oauth/token                            code exchange + refresh rotation
  GET|POST /signup, /verify, /login, /consent  the passkey ceremonies and pages

Hand-rolled routing on the same pure-ASGI footing as `auth_middleware`: no new
dependency, no framework indirection, and the whole surface is enumerable above.

Pages are deliberately plain HTML with a little inline JavaScript — WebAuthn ceremonies
must run in the browser, but nothing here needs a build step, and a page that is a single
readable file is a page an operator can audit.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, unquote_plus

from sqlalchemy.ext.asyncio import AsyncEngine

from .accounts import AccountSummary, erase, summarise
from .email import EmailSender
from .oauth import (
    FatalAuthorizeError,
    OAuthError,
    OAuthService,
    RedirectAuthorizeError,
)
from .passkeys import (
    SESSION_COOKIE,
    PasskeyError,
    PasskeyService,
    WebSession,
    constant_time_equals,
)
from .ratelimit import FixedWindowLimiter

Handler = Callable[[dict[str, Any], bytes], Awaitable[tuple[int, dict[str, str], bytes]]]

logger = logging.getLogger(__name__)

_PAGE_STYLE = """
<style>
 body{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:4rem auto;padding:0 1rem;color:#111}
 button{font:inherit;padding:.6rem 1.1rem;border:0;border-radius:.4rem;
   background:#1a5fb4;color:#fff;cursor:pointer}
 input{font:inherit;padding:.5rem;width:100%;box-sizing:border-box;margin:.4rem 0 1rem}
 .msg{padding:.75rem 1rem;border-radius:.4rem;background:#f6f5f4;margin:1rem 0}
 .err{background:#f9e0e0}
 code{background:#f6f5f4;padding:.1rem .3rem;border-radius:.2rem}
</style>
"""

# Shared browser helper: WebAuthn speaks ArrayBuffers, JSON speaks base64url.
_WEBAUTHN_JS = """
<script>
const b64ToBuf = s =>
  Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0));
const bufToB64 = b =>
  btoa(String.fromCharCode(...new Uint8Array(b)))
    .replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
function showError(e){ document.getElementById('msg').innerHTML =
  '<div class="msg err">' + (e && e.message ? e.message : e) + '</div>'; }
async function postJSON(url, body){
  const r = await fetch(url, {method:'POST', headers:{'content-type':'application/json'},
                             body: JSON.stringify(body)});
  const data = await r.json();
  if(!r.ok) throw new Error(data.error || 'request failed');
  return data;
}
</script>
"""


def _html(body: str, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    page = f"<!doctype html><meta charset=utf-8><title>PSE Edge MCP</title>{_PAGE_STYLE}{body}"
    return status, {"content-type": "text/html; charset=utf-8"}, page.encode()


def _json_response(
    payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    return (
        status,
        {"content-type": "application/json", **(headers or {})},
        json.dumps(payload).encode(),
    )


def _redirect(url: str, cookie: str | None = None) -> tuple[int, dict[str, str], bytes]:
    headers = {"location": url}
    if cookie:
        headers["set-cookie"] = cookie
    return 302, headers, b""


def _session_cookie(sid: str, secure: bool) -> str:
    # HttpOnly so script cannot read it; SameSite=Lax so the OAuth redirect back from the
    # client still carries it; Secure whenever the public URL is https.
    flags = "; Secure" if secure else ""
    return f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax{flags}"


def _peer_ip(scope: dict[str, Any]) -> str | None:
    """The caller's address, honouring X-Forwarded-For.

    Behind Caddy or a Cloudflare Tunnel every request arrives from the proxy, so the
    connection address alone would lump the whole internet into one rate-limit bucket. The
    LEFTMOST XFF entry is the original client. It is client-controlled and therefore
    spoofable — which is acceptable for this use: the per-client_id key still holds when the
    IP key is evaded, and the alternative (one shared bucket for all traffic) is strictly
    worse, since anyone could then lock out every other caller.
    """
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    client = scope.get("client")
    return str(client[0]) if client else None


def _basic_auth_client_id(authorization: str | None) -> str | None:
    """The client_id from a Basic header, for rate-limit keying only.

    Parsed leniently and never used for authentication — `oauth._client_auth` does that
    properly. This only needs a stable string to count against.
    """
    if not authorization or not authorization.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return unquote_plus(raw.partition(":")[0])[:200] or None


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return str(value.decode("latin-1"))
    return None


def _cookies(scope: dict[str, Any]) -> dict[str, str]:
    raw = ""
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            raw = value.decode("latin-1")
            break
    jar: dict[str, str] = {}
    for part in raw.split(";"):
        key, _, val = part.strip().partition("=")
        if key:
            jar[key] = val
    return jar


def _query(scope: dict[str, Any]) -> dict[str, str]:
    raw = scope.get("query_string", b"").decode("latin-1")
    return {k: v[0] for k, v in parse_qs(raw).items()}


class AuthApp:
    """Routes the auth surface; delegates anything else to the wrapped app."""

    def __init__(
        self,
        app: Any,
        *,
        oauth: OAuthService,
        passkeys: PasskeyService,
        email: EmailSender,
        public_url: str,
        engine: AsyncEngine,
        token_limiter: FixedWindowLimiter | None = None,
    ) -> None:
        self._engine = engine
        self._app = app
        self._oauth = oauth
        self._passkeys = passkeys
        self._email = email
        self._public = public_url.rstrip("/")
        self._secure = self._public.startswith("https://")
        # 20 token requests per client_id and per IP per minute. Generous for a legitimate
        # agent (a machine token lasts an hour, so one request per hour is the norm) and
        # far below what online guessing would need against a 48-byte secret.
        self._token_limiter = token_limiter or FixedWindowLimiter(limit=20, window_sec=60)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        handler = self._route(scope["path"], scope["method"])
        if handler is None:
            await self._app(scope, receive, send)
            return

        body = await _read_body(receive)
        try:
            status, headers, payload = await handler(scope, body)
        except OAuthError as exc:
            # RFC 6749 §5.2: a 401 from the token endpoint MUST carry WWW-Authenticate, so
            # a client can tell "your credentials were rejected" from a generic failure.
            extra = (
                {"www-authenticate": 'Basic realm="oauth", charset="UTF-8"'}
                if exc.status == 401
                else None
            )
            status, headers, payload = _json_response(exc.payload(), exc.status, extra)
        except PasskeyError as exc:
            status, headers, payload = _json_response({"error": str(exc)}, 400)
        await _send(send, status, headers, payload)

    def _route(self, path: str, method: str) -> Handler | None:
        table: dict[tuple[str, str], Handler] = {
            ("/", "GET"): self._index,
            ("/favicon.ico", "GET"): self._favicon,
            ("/.well-known/oauth-protected-resource", "GET"): self._protected_resource_metadata,
            ("/.well-known/oauth-authorization-server", "GET"): self._as_metadata,
            ("/oauth/register", "POST"): self._register,
            ("/oauth/authorize", "GET"): self._authorize,
            ("/oauth/token", "POST"): self._token,
            ("/signup", "GET"): self._signup_page,
            ("/signup", "POST"): self._signup_submit,
            ("/verify", "GET"): self._verify,
            ("/enroll", "GET"): self._enroll_page,
            ("/enroll/options", "POST"): self._enroll_options,
            ("/enroll/finish", "POST"): self._enroll_finish,
            ("/login", "GET"): self._login_page,
            ("/login/options", "POST"): self._login_options,
            ("/login/finish", "POST"): self._login_finish,
            ("/consent", "POST"): self._consent,
            ("/privacy", "GET"): self._privacy,
            ("/account", "GET"): self._account,
            ("/account/delete", "POST"): self._account_delete,
        }
        return table.get((path, method))

    # --- front door ----------------------------------------------------------

    async def _index(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """A page for people at `/`.

        Everything not in this table falls through to the MCP endpoint, which answers a
        browser with `{"error": "UNAUTHORIZED"}`. That is the right answer for a client
        calling an API without a token and the wrong one for a person typing the hostname,
        who is told they are missing something they have no way to obtain.
        """
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if session and session.kind == "authenticated" and session.user_id:
            return _redirect(f"{self._public}/account")
        return _html(
            "<h1>PSE Edge MCP</h1>"
            "<p>End-of-day Philippine Stock Exchange data over the Model Context "
            "Protocol.</p>"
            "<p>Point an MCP client at this server and it will bring you back here to "
            "authorize — you do not need to sign up first:</p>"
            f"<pre>{self._public}/mcp</pre>"
            "<p><a href='/signup'>Create an account</a> · <a href='/login'>Sign in</a> · "
            "<a href='/privacy'>Privacy</a></p>"
        )

    async def _favicon(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        # 204 rather than a fall-through 401: every browser asks for this, and the refusals
        # are pure noise in a log someone is reading to debug something real.
        return 204, {}, b""

    # --- metadata ------------------------------------------------------------

    async def _protected_resource_metadata(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        # RFC 9728: how an MCP client discovers which AS guards this resource.
        return _json_response(
            {
                "resource": f"{self._public}/mcp",
                "authorization_servers": [self._public],
                "bearer_methods_supported": ["header"],
            }
        )

    async def _as_metadata(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        return _json_response(
            {
                "issuer": self._public,
                "authorization_endpoint": f"{self._public}/oauth/authorize",
                "token_endpoint": f"{self._public}/oauth/token",
                "registration_endpoint": f"{self._public}/oauth/register",
                "response_types_supported": ["code"],
                # client_credentials is advertised because a machine client must be able to
                # discover it. Advertising is not authorization: the grant is refused for
                # every client except those an admin provisioned as `machine`, so a DCR
                # client reading this list and trying it still gets `unauthorized_client`.
                "grant_types_supported": [
                    "authorization_code",
                    "refresh_token",
                    "client_credentials",
                ],
                "code_challenge_methods_supported": ["S256"],  # S256 only, never plain
                # "none" for the public DCR clients that use PKCE instead of a secret; the
                # two secret-based methods for provisioned machine clients.
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_basic",
                    "client_secret_post",
                ],
                "scopes_supported": ["mcp"],
            }
        )

    # --- OAuth ---------------------------------------------------------------

    async def _register(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise OAuthError("invalid_client_metadata", "body is not valid JSON") from exc
        return _json_response(await self._oauth.register_client(payload), 201)

    async def _authorize(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        params = _query(scope)
        # Returning from the login page: the flow already exists, so resume it rather
        # than starting a second one (which would strand the first).
        if "flow" in params and "client_id" not in params:
            flow = await self._oauth.load_flow(params["flow"])
            if flow is None:
                return _html("<h1>That authorization request expired</h1>", 400)
            session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
            if session and session.kind == "authenticated" and session.user_id:
                return _html(
                    _consent_page(
                        flow.client_name, flow.flow_id, session.email or "", session.csrf_token
                    )
                )
            return _redirect(f"{self._public}/login?flow={flow.flow_id}")
        try:
            flow = await self._oauth.begin_authorize(params)
        except FatalAuthorizeError as exc:
            # Never redirect on these: an unvalidated redirect_uri would make this an
            # open redirector.
            return _html(f"<h1>Cannot continue</h1><div class='msg err'>{exc}</div>", 400)
        except RedirectAuthorizeError as exc:
            return _redirect(exc.redirect_url)

        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if session and session.kind == "authenticated" and session.user_id:
            return _html(
                _consent_page(
                    flow.client_name, flow.flow_id, session.email or "", session.csrf_token
                )
            )
        return _redirect(f"{self._public}/login?flow={flow.flow_id}")

    async def _token(self, scope: dict[str, Any], body: bytes) -> tuple[int, dict[str, str], bytes]:
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        # Rate-limited before the secret is even looked at: this endpoint is the one place
        # a long-lived client secret can be guessed online, and the limiter is what turns
        # that from "keep trying" into "come back later". Keyed on client_id AND the peer
        # address, so one noisy client cannot exhaust another's budget and a single host
        # cannot spray attempts across many client ids.
        client_id = form.get("client_id") or _basic_auth_client_id(
            _header(scope, b"authorization")
        )
        retry_after = self._token_limiter.check(client_id, _peer_ip(scope))
        if retry_after is not None:
            return _json_response(
                {
                    "error": "slow_down",
                    "error_description": "too many token requests; retry shortly",
                },
                429,
                {"retry-after": str(retry_after), "cache-control": "no-store"},
            )
        result = await self._oauth.exchange(form, authorization=_header(scope, b"authorization"))
        # RFC 6749 §5.1: token responses must not be cached anywhere.
        return _json_response(result, headers={"cache-control": "no-store", "pragma": "no-cache"})

    async def _consent(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if not session or session.kind != "authenticated" or not session.user_id:
            return _html("<h1>Session expired</h1><p>Start the authorization again.</p>", 403)
        if not constant_time_equals(form.get("csrf_token", ""), session.csrf_token):
            return _html("<h1>Invalid request</h1><p>Start the authorization again.</p>", 403)
        redirect_url = await self._oauth.issue_code(form.get("flow_id", ""), session.user_id)
        return _redirect(redirect_url)

    # --- signup / verify -----------------------------------------------------

    async def _signup_page(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        return _html(
            "<h1>Create an account</h1>"
            "<p>We email you a link; you then enroll a passkey. No passwords.</p>"
            "<form method=post action='/signup'>"
            "<label>Email<input name=email type=email required autofocus></label>"
            "<button type=submit>Send verification link</button></form>"
            "<p style='font-size:.9em'>We store your email address and aggregated usage "
            "counts only — see the <a href='/privacy'>privacy page</a>. You can delete "
            "your account yourself at any time.</p>"
        )

    async def _signup_submit(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        email = form.get("email", "")
        try:
            token = await self._passkeys.start_signup(email)
        except PasskeyError as exc:
            return _html(f"<h1>Create an account</h1><div class='msg err'>{exc}</div>", 400)
        link = f"{self._public}/verify?token={token}"
        try:
            await self._email.send(
                to=email.strip().lower(),
                subject="Verify your PSE Edge MCP account",
                html=(
                    f'<p>Confirm this address to finish signing up:</p>'
                    f'<p><a href="{link}">{link}</a>'
                    "</p><p>The link expires in 30 minutes. If you did not request it, ignore "
                    "this email.</p>"
                ),
            )
        except Exception:
            # A mail provider having a bad day is not this user's bug to read a stack trace
            # about. 503 with a retry suggestion: the signup token is already stored, so
            # trying again in a minute genuinely can work. The detail goes to the log, where
            # the operator can act on it — it may name the misconfigured sender address.
            logger.exception("signup email failed; the operator needs to see this")
            return _html(
                "<h1>Check your email</h1><div class='msg err'>We could not send the "
                "verification email just now. This is our problem, not yours — please try "
                "again in a few minutes.</div>",
                503,
            )
        # Always the same response, whether or not the address is already registered:
        # otherwise this page enumerates accounts.
        return _html(
            "<h1>Check your email</h1><div class=msg>If that address can receive mail, "
            "a verification link is on its way. It expires in 30 minutes.</div>"
        )

    async def _verify(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        token = _query(scope).get("token", "")
        session = await self._passkeys.consume_verification(token)
        return _redirect(f"{self._public}/enroll", _session_cookie(session.sid, self._secure))

    # --- passkey enrollment --------------------------------------------------

    async def _enroll_page(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        return _html(_enroll_page_html() + _WEBAUTHN_JS)

    async def _enroll_options(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        session = await self._require_session(scope)
        return _json_response(await self._passkeys.begin_enrollment(session))

    async def _enroll_finish(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        session = await self._require_session(scope)
        await self._passkeys.finish_enrollment(session, json.loads(body))
        return _json_response({"ok": True, "next": f"{self._public}/login"})

    # --- passkey login -------------------------------------------------------

    async def _login_page(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        flow = _query(scope).get("flow", "")
        return _html(_login_page_html(flow) + _WEBAUTHN_JS)

    async def _login_options(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        session, options = await self._passkeys.begin_login()
        return _json_response(
            options, headers={"set-cookie": _session_cookie(session.sid, self._secure)}
        )

    async def _login_finish(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        session = await self._require_session(scope)
        payload = json.loads(body)
        await self._passkeys.finish_login(session, payload["credential"])
        flow_id = payload.get("flow_id") or ""
        # No flow means a person signed in directly rather than being sent by an MCP client,
        # so send them somewhere built for a person. This used to be the bare public URL,
        # which is not a page — it fell through to the MCP app and answered a freshly
        # authenticated user with "Missing bearer token".
        next_url = (
            f"{self._public}/oauth/authorize?flow={flow_id}"
            if flow_id
            else f"{self._public}/account"
        )
        return _json_response({"ok": True, "next": next_url, "flow_id": flow_id})

    # --- privacy & account (plan §6a) ----------------------------------------

    async def _privacy(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        # Deliberately unauthenticated: a privacy policy nobody can read before signing up
        # is not a privacy policy.
        return _html(PRIVACY_POLICY)

    async def _account(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if not session or session.kind != "authenticated" or not session.user_id:
            return _redirect(f"{self._public}/login")
        summary = await summarise(self._engine, session.user_id)
        return _html(_account_page(summary, session.csrf_token))

    async def _account_delete(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Self-service erasure (plan §6a): immediate, complete, and needs no approval."""
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if not session or session.kind != "authenticated" or not session.user_id:
            return _redirect(f"{self._public}/login")
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        if not constant_time_equals(form.get("csrf_token", ""), session.csrf_token):
            return _html(
                "<h1>Invalid request</h1><p>Please try again from your account page.</p>", 403
            )

        removed = await erase(self._engine, session.user_id)
        # Clear the cookie: the session row is gone, so leaving it set would only produce
        # confusing "session expired" pages.
        cleared = f"{SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax"
        return (
            200,
            {"content-type": "text/html; charset=utf-8", "set-cookie": cleared},
            _html(
                "<h1>Account deleted</h1><div class=msg>Your account, passkeys, tokens and "
                f"usage history have been erased ({sum(removed.values())} records). "
                "Nothing identifying you remains.</div>"
            )[2],
        )

    async def _require_session(self, scope: dict[str, Any]) -> WebSession:
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if session is None:
            raise PasskeyError("your session expired — start again")
        return session


def _consent_page(client_name: str, flow_id: str, email: str, csrf_token: str) -> str:
    return (
        f"<h1>Authorize {client_name}</h1>"
        f"<p>Signed in as <code>{email}</code>.</p>"
        f"<p>{client_name} is requesting access to PSE Edge market data on your behalf. "
        "It will be able to call this server's tools using your account's quota.</p>"
        f"<form method=post action='/consent'>"
        f"<input type=hidden name=flow_id value='{flow_id}'>"
        f"<input type=hidden name=csrf_token value='{csrf_token}'>"
        "<button type=submit>Allow</button></form>"
    )


def _enroll_page_html() -> str:
    return """
<h1>Enroll a passkey</h1>
<p>Your email is verified. Create a passkey to finish — a fingerprint, face or device PIN.</p>
<div id=msg></div>
<button id=go>Create passkey</button>
<script>
document.getElementById('go').onclick = async () => {
  try {
    const opts = await postJSON('/enroll/options', {});
    opts.challenge = b64ToBuf(opts.challenge);
    opts.user.id = b64ToBuf(opts.user.id);
    (opts.excludeCredentials || []).forEach(c => c.id = b64ToBuf(c.id));
    const cred = await navigator.credentials.create({publicKey: opts});
    const out = await postJSON('/enroll/finish', {
      id: cred.id, rawId: bufToB64(cred.rawId), type: cred.type,
      response: {
        clientDataJSON: bufToB64(cred.response.clientDataJSON),
        attestationObject: bufToB64(cred.response.attestationObject)
      }
    });
    document.getElementById('msg').innerHTML =
      '<div class=msg>Passkey enrolled. You can close this tab or ' +
      '<a href="' + out.next + '">sign in</a>.</div>';
  } catch (e) { showError(e); }
};
</script>
"""


def _login_page_html(flow_id: str) -> str:
    return f"""
<h1>Sign in</h1>
<p>Use the passkey you enrolled.</p>
<div id=msg></div>
<button id=go>Sign in with a passkey</button>
<script>
const flowId = {json.dumps(flow_id)};
document.getElementById('go').onclick = async () => {{
  try {{
    const opts = await postJSON('/login/options', {{}});
    opts.challenge = b64ToBuf(opts.challenge);
    (opts.allowCredentials || []).forEach(c => c.id = b64ToBuf(c.id));
    const cred = await navigator.credentials.get({{publicKey: opts}});
    const out = await postJSON('/login/finish', {{
      flow_id: flowId,
      credential: {{
        id: cred.id, rawId: bufToB64(cred.rawId), type: cred.type,
        response: {{
          clientDataJSON: bufToB64(cred.response.clientDataJSON),
          authenticatorData: bufToB64(cred.response.authenticatorData),
          signature: bufToB64(cred.response.signature),
          userHandle: cred.response.userHandle ? bufToB64(cred.response.userHandle) : null
        }}
      }}
    }});
    window.location = out.next;
  }} catch (e) {{ showError(e); }}
}};
</script>
"""


async def _read_body(receive: Any) -> bytes:
    chunks = b""
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks += message.get("body", b"")
        if not message.get("more_body"):
            break
    return chunks


async def _send(send: Any, status: int, headers: dict[str, str], body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                *[(k.encode(), v.encode()) for k, v in headers.items()],
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


PRIVACY_POLICY = """
<h1>Privacy</h1>
<p>This server is an unofficial interface to public PSE Edge market data. This page
describes what it holds about <em>you</em>, which is deliberately very little.</p>

<h2>What is collected</h2>
<ul>
  <li><strong>Your email address.</strong> It is the account identifier and the only way to
      recover access if you lose your passkeys. Nothing else identifying is collected — no
      name, no phone number, no payment details.</li>
  <li><strong>Passkey public keys.</strong> Public keys and usage counters only. A passkey's
      private key never leaves your device, and this server could not obtain it.</li>
  <li><strong>Usage counts.</strong> How many requests your account made, aggregated per
      hour — not which tools you called, not which companies you looked up, and not the
      content of any request.</li>
</ul>
<p>There are no passwords, because the service never uses them. Access tokens are stored
only as irreversible hashes.</p>

<h2>How long it is kept</h2>
<p>Usage counts are deleted automatically after <strong>90 days</strong>. Everything else is
kept until you delete your account.</p>

<h2>Your rights</h2>
<p>Sign in and visit <a href="/account">your account page</a> to see everything held about
you and to <strong>delete your account immediately</strong> — no request, no waiting period,
no email exchange. Deletion removes your account, your passkeys, your tokens and your usage
history in a single operation and cannot be undone.</p>
<p>Market data (prices and disclosures) that passed through your requests is retained. It is
public information published by the PSE and is not about you.</p>

<h2>Who to contact</h2>
<p>For privacy questions or to report a suspected data breach, contact the operator at the
address published on the repository: <a
href="https://github.com/phdwight/pse-edge-mcp">github.com/phdwight/pse-edge-mcp</a>.
Under the PH Data Privacy Act, breaches meeting the notification threshold are reported to
the National Privacy Commission and to affected users.</p>

<h2>Third parties</h2>
<p>Verification emails are delivered by ZeptoMail (Zoho), which receives your address for
that purpose. PSE Edge itself receives no information about you: this server requests public
market pages on its own behalf, never yours.</p>
"""


def _account_page(summary: AccountSummary, csrf_token: str) -> str:
    """The subject-access view plus the deletion control, on one page."""
    rows = "".join(
        f"<tr><td>{day['day']}</td><td>{day['requests']}</td><td>{day['rejected']}</td></tr>"
        for day in summary.usage_days[:30]
    )
    usage_table = (
        f"<table><tr><th>Day</th><th>Requests</th><th>Refused</th></tr>{rows}</table>"
        if rows
        else "<p>No usage recorded yet.</p>"
    )
    return f"""
<h1>Your account</h1>
<div class=msg>
  <p><strong>Email:</strong> <code>{summary.email}</code></p>
  <p><strong>Member since:</strong> {summary.created_at:%Y-%m-%d}</p>
  <p><strong>Passkeys:</strong> {summary.passkeys} &nbsp;
     <strong>Active tokens:</strong> {summary.active_tokens}</p>
</div>
<h2>Usage (kept 90 days)</h2>
{usage_table}
<h2>Delete your account</h2>
<p>This removes your account, passkeys, tokens and usage history immediately and
permanently. It cannot be undone. See the <a href="/privacy">privacy page</a>.</p>
<form method=post action='/account/delete'
      onsubmit="return confirm('Permanently delete your account? This cannot be undone.')">
  <input type=hidden name=csrf_token value='{csrf_token}'>
  <button type=submit style="background:#c01c28">Delete my account</button>
</form>
"""
