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
import html
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, unquote_plus

from sqlalchemy.ext.asyncio import AsyncEngine

from .accounts import AccountSummary, erase, revoke_session, summarise
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
 *{box-sizing:border-box}
 body{font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;color:#1c1b2e;
   min-height:100vh;background:linear-gradient(115deg,#cdc6f2 0%,#e4e0fa 30%,#eef0f7 55%,
   #cfe9ee 100%) fixed}
 a{color:#4f46e5;text-decoration:none}
 a:hover{text-decoration:underline}
 code{background:#edeaf9;padding:.12rem .4rem;border-radius:.35rem;font-size:.92em}
 pre{background:#f4f2fc;padding:1rem;border-radius:.75rem;overflow-x:auto}
 .topbar{display:flex;align-items:center;gap:.7rem;padding:.7rem 1.5rem;position:sticky;top:0;
   background:rgba(255,255,255,.72);backdrop-filter:blur(10px);
   border-bottom:1px solid rgba(28,27,46,.08);z-index:10}
 .brand{display:flex;align-items:center;gap:.6rem;font-weight:800;font-size:1.1rem;
   color:#1c1b2e !important;text-decoration:none !important}
 .brand .mark{width:1.6rem;height:1.6rem;border-radius:.5rem;
   background:linear-gradient(135deg,#8b83f6,#5b54e8)}
 .topnav{margin-left:auto;display:flex;align-items:center;gap:1.3rem}
 .topnav a{color:#4b4a63;font-weight:500}
 .topnav a.current{background:#fff;padding:.45rem 1rem;border-radius:.7rem;
   box-shadow:0 1px 5px rgba(28,27,46,.14);color:#1c1b2e;font-weight:600}
 main{max-width:64rem;margin:0 auto;padding:2.5rem 1.5rem 4rem}
 main.narrow{max-width:30rem;padding-top:4rem}
 .eyebrow{letter-spacing:.18em;text-transform:uppercase;font-size:.78rem;font-weight:700;
   color:#6d66e8;margin:0 0 .3rem}
 h1{font-size:2.3rem;font-weight:800;margin:.1rem 0 1rem;letter-spacing:-.02em}
 .pagehead{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;
   gap:1rem}
 .pagehead .who{text-align:right;color:#4b4a63;font-size:.95rem}
 .card{background:rgba(255,255,255,.66);border:1px solid rgba(255,255,255,.85);
   border-radius:1.1rem;box-shadow:0 8px 24px rgba(60,55,120,.08);padding:1.2rem 1.5rem;
   margin:1rem 0}
 .card.dangerzone{background:rgba(252,236,238,.72)}
 .tabs{display:flex;gap:.3rem;background:rgba(255,255,255,.6);border-radius:1rem;
   padding:.35rem;box-shadow:0 4px 16px rgba(60,55,120,.08);margin:1.6rem 0 .6rem;
   flex-wrap:wrap}
 .tabs a{flex:1;text-align:center;padding:.6rem .9rem;border-radius:.8rem;color:#3b3a52;
   font-weight:600;white-space:nowrap}
 .tabs a:hover{text-decoration:none;background:rgba(255,255,255,.7)}
 .tabs a.active{background:#fff;box-shadow:0 2px 8px rgba(28,27,46,.14);color:#111}
 .tabs a.tab-danger{color:#c02636}
 html.js section.tab{display:none}
 html.js section.tab.active{display:block}
 .sectionhead{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;
   border-bottom:1px solid rgba(28,27,46,.12);padding-bottom:.45rem;margin:1.8rem 0 .2rem}
 .sectionhead h2{margin:0;font-size:1.3rem;font-weight:700}
 .sectionhead .aside{color:#6a687f;font-size:.9rem}
 h2{font-size:1.3rem;font-weight:700}
 table{width:100%;border-collapse:collapse}
 th{font-size:.76rem;letter-spacing:.08em;text-transform:uppercase;color:#6a687f;
   text-align:left;padding:.5rem .6rem;border-bottom:1px solid rgba(28,27,46,.1)}
 td{padding:.75rem .6rem;border-bottom:1px solid rgba(28,27,46,.07)}
 tr:last-child td{border-bottom:0}
 .row{display:flex;align-items:center;justify-content:space-between;gap:1rem;
   padding:.85rem 0;border-bottom:1px solid rgba(28,27,46,.08)}
 .row:last-child{border-bottom:0}
 .row .grow{flex:1}
 button{font:inherit;font-weight:600;padding:.6rem 1.2rem;border:0;border-radius:.75rem;
   background:#5b54e8;color:#fff;cursor:pointer;box-shadow:0 2px 8px rgba(91,84,232,.35)}
 button:hover{background:#4f48d6}
 button.danger{background:#c9333f;box-shadow:0 2px 8px rgba(201,51,63,.3)}
 button.danger:hover{background:#b32b37}
 button.ghost{background:#fff;color:#1c1b2e;border:1px solid rgba(28,27,46,.15);
   box-shadow:0 1px 3px rgba(28,27,46,.08)}
 button.ghost:hover{background:#f6f5fb}
 input{font:inherit;padding:.6rem .8rem;border:1px solid rgba(28,27,46,.18);
   border-radius:.75rem;background:#fff;width:100%;max-width:24rem;margin:.4rem 0 1rem}
 label{font-weight:600}
 .chip{display:inline-block;padding:.15rem .6rem;border-radius:1rem;font-size:.8rem;
   font-weight:600;background:#d9f2df;color:#1a7a3a;vertical-align:middle}
 .chip.warn{background:#faeed2;color:#946200}
 .muted{color:#6a687f}
 .btnlink{display:inline-block;font-weight:600;padding:.6rem 1.2rem;border-radius:.75rem;
   background:#fff;border:1px solid rgba(28,27,46,.15);color:#1c1b2e !important;
   box-shadow:0 1px 3px rgba(28,27,46,.08)}
 .btnlink:hover{background:#f6f5fb;text-decoration:none !important}
 .msg{padding:.8rem 1.1rem;border-radius:.75rem;background:rgba(255,255,255,.75);
   border:1px solid rgba(255,255,255,.9);margin:1rem 0}
 .err{background:#f9e0e0;border-color:#f1c7c7}
</style>
"""

_TOPBAR = """
<header class=topbar>
  <a class=brand href="/"><span class=mark></span>PSE Edge MCP</a>
  <nav class=topnav>
    <a href="/privacy">Privacy</a>
    <a class=current href="/account">Account</a>
  </nav>
</header>
"""

# Tab switching for the account page. Progressive enhancement: without JavaScript the
# `html.js` CSS never applies and every section simply renders stacked, forms included.
_TABS_JS = """
<script>
function showTab(id){
  document.querySelectorAll('section.tab').forEach(s =>
    s.classList.toggle('active', s.id === id));
  document.querySelectorAll('.tabs a').forEach(a =>
    a.classList.toggle('active', a.getAttribute('href') === '#' + id));
}
function currentTab(){
  const id = location.hash.slice(1);
  const first = document.querySelector('section.tab');
  return document.getElementById(id) ? id : (first ? first.id : '');
}
addEventListener('DOMContentLoaded', () => showTab(currentTab()));
addEventListener('hashchange', () => showTab(currentTab()));
</script>
"""


def _fmt_date(value: Any) -> str:
    try:
        return f"{value:%b} {value.day}, {value:%Y}"
    except (AttributeError, ValueError, TypeError):
        return str(value)

# Shared browser helper: WebAuthn speaks ArrayBuffers, JSON speaks base64url.
_WEBAUTHN_JS = """
<script>
const b64ToBuf = s =>
  Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0));
const bufToB64 = b =>
  btoa(String.fromCharCode(...new Uint8Array(b)))
    .replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
function showError(e){
  let m = (e && e.message ? e.message : String(e));
  if (e && e.name === 'NotAllowedError') {
    // The platform's own wording ("not allowed by the user agent...") is useless to a
    // person. The overwhelmingly common cause on mobile is an email app's built-in
    // browser, which cannot create passkeys — seen in production on day one.
    m = 'Your browser refused the passkey prompt. If you opened this page from an ' +
        'email app, its built-in browser usually cannot create passkeys: open ' +
        '<b>' + location.host + '</b> in Safari or Chrome and sign up again ' +
        '(email links are single-use). If a fingerprint or face prompt appeared ' +
        'and was dismissed, simply press the button again.';
  }
  document.getElementById('msg').innerHTML = '<div class="msg err">' + m + '</div>';
}
// Say so up front when this context cannot do WebAuthn at all, instead of letting the
// button fail with a mystery. #go exists only on the enroll and login pages.
addEventListener('DOMContentLoaded', () => {
  const go = document.getElementById('go');
  if (go && !window.PublicKeyCredential) {
    go.disabled = true;
    document.getElementById('msg').innerHTML =
      '<div class="msg err">This browser cannot create passkeys (no WebAuthn). ' +
      'Open <b>' + location.host + '</b> in Safari or Chrome and sign up again — ' +
      'email links are single-use, so you will need a fresh one.</div>';
  }
});
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
    # Pages that don't lay themselves out (error fragments, simple notices) get the
    # narrow centered column; full pages start with their own <main>.
    if not body.lstrip().startswith("<main"):
        body = f"<main class=narrow>{body}</main>"
    page = (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>PSE Edge MCP</title>{_PAGE_STYLE}"
        "<script>document.documentElement.classList.add('js')</script>"
        f"</head><body>{_TOPBAR}{body}</body></html>"
    )
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
        admin_emails: frozenset[str] | None = None,
        token_limiter: FixedWindowLimiter | None = None,
    ) -> None:
        self._engine = engine
        self._app = app
        self._oauth = oauth
        self._passkeys = passkeys
        self._email = email
        self._public = public_url.rstrip("/")
        self._secure = self._public.startswith("https://")
        # Accounts allowed to provision machine clients from /account. Stored lowercased so
        # the comparison matches `users.email`, which is stored lowercased. Empty = the web
        # provisioning surface does not exist and the CLI is the only route.
        self._admin_emails = frozenset(e.lower() for e in (admin_emails or frozenset()))
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
            ("/account/sessions/revoke", "POST"): self._revoke_session,
            ("/account/machine-clients", "POST"): self._create_machine_client,
            ("/account/machine-clients/revoke", "POST"): self._revoke_machine_client,
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
            "<main class=narrow>"
            "<p class=eyebrow>Unofficial · End of day</p>"
            "<h1>PSE Edge MCP</h1>"
            "<div class=card>"
            "<p>End-of-day Philippine Stock Exchange data over the Model Context "
            "Protocol.</p>"
            "<p>Point an MCP client at this server and it will bring you back here to "
            "authorize — you do not need to sign up first:</p>"
            f"<pre>{self._public}/mcp</pre>"
            "<p style='margin-bottom:0'><a class=btnlink href='/signup'>Create an account</a> "
            "&nbsp;<a class=btnlink href='/login'>Sign in</a></p>"
            "</div>"
            "<p class=muted>See the <a href='/privacy'>privacy page</a> for what this "
            "server holds about you — deliberately very little.</p>"
            "</main>"
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
            "<main class=narrow>"
            "<p class=eyebrow>Sign up</p>"
            "<h1 style='font-size:1.7rem'>Create an account</h1>"
            "<div class=card>"
            "<p style='margin-top:0'>We email you a link; you then enroll a passkey. "
            "No passwords.</p>"
            "<form method=post action='/signup' style='margin:0'>"
            "<label>Email<input name=email type=email required autofocus></label>"
            "<button type=submit>Send verification link</button></form>"
            "</div>"
            "<p class=muted style='font-size:.9em'>We store your email address and "
            "aggregated usage counts only — see the <a href='/privacy'>privacy page</a>. "
            "You can delete your account yourself at any time.</p>"
            "</main>"
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

    def _is_admin(self, session: WebSession) -> bool:
        return bool(session.email and session.email.lower() in self._admin_emails)

    async def _account(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if not session or session.kind != "authenticated" or not session.user_id:
            return _redirect(f"{self._public}/login")
        summary = await summarise(self._engine, session.user_id)
        # The machine-client panel appears only for operators (PSE_ADMIN_EMAILS). A normal
        # account never sees it and cannot reach the routes behind it — the same authority
        # as the admin CLI, so it is deliberately not self-service.
        machine_panel = ""
        if self._is_admin(session):
            from .admin import list_machine_clients, machine_client_request_totals

            clients = await list_machine_clients(self._engine)
            totals = await machine_client_request_totals(self._engine)
            machine_panel = _machine_clients_panel(clients, session.csrf_token, totals)
        return _html(_account_page(summary, session.csrf_token, machine_panel))

    async def _revoke_session(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Self-service sign-out of one connected client: revoke its token family.

        `revoke_session` is scoped to the caller's own user_id, so the family_id from the
        form — attacker-suppliable like any form field — can only ever kill the caller's
        own session.
        """
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if not session or session.kind != "authenticated" or not session.user_id:
            return _redirect(f"{self._public}/login")
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        if not constant_time_equals(form.get("csrf_token", ""), session.csrf_token):
            return _html(
                "<h1>Invalid request</h1><p>Please try again from your account page.</p>", 403
            )
        revoked = await revoke_session(
            self._engine, session.user_id, form.get("family_id", "")
        )
        logger.info("web: a user revoked one of their token sessions (%d rows)", revoked)
        return _redirect(f"{self._public}/account#security")

    async def _admin_form(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[WebSession, dict[str, str]] | tuple[int, dict[str, str], bytes]:
        """Shared guard for the machine-client routes: authenticated + operator + CSRF.

        Returns the session and parsed form on success, or a ready-to-send refusal. The
        403 for a non-operator is deliberately identical to the not-signed-in redirect's
        end state — a normal account gets no signal that this surface exists.
        """
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if not session or session.kind != "authenticated" or not session.user_id:
            return _redirect(f"{self._public}/login")
        if not self._is_admin(session):
            return _html("<h1>Not found</h1>", 404)
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        if not constant_time_equals(form.get("csrf_token", ""), session.csrf_token):
            return _html("<h1>Invalid request</h1><p>Return to your account page.</p>", 403)
        return session, form

    async def _create_machine_client(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        guard = await self._admin_form(scope, body)
        if len(guard) == 3:
            return guard  # a refusal
        _, form = guard
        from .admin import AdminError, create_machine_client

        name = (form.get("name") or "").strip()
        try:
            created = await create_machine_client(self._engine, name or "machine-client")
        except AdminError as exc:
            return _html(
                "<h1>Could not create client</h1>"
                f"<div class='msg err'>{html.escape(str(exc))}</div>",
                400,
            )
        logger.info(
            "machine client %s provisioned from the account page by an operator",
            created["client_id"],
        )
        # The secret is rendered here and never again — only its hash is stored. Deliberately
        # the whole response, so it cannot be missed, with the token endpoint spelled out so
        # the operator can hand the two values straight to an agent. no-store for the same
        # reason the token endpoint sets it (RFC 6749 §5.1): a cached copy of this page IS
        # the credential.
        status, headers, page = _html(_machine_client_created_page(created, self._public))
        return status, {**headers, "cache-control": "no-store"}, page

    async def _revoke_machine_client(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        guard = await self._admin_form(scope, body)
        if len(guard) == 3:
            return guard
        _, form = guard
        from .admin import AdminError, revoke_machine_client

        client_id = (form.get("client_id") or "").strip()
        try:
            await revoke_machine_client(self._engine, client_id)
        except AdminError as exc:
            # exc can echo the submitted client_id; escape it. CSRF already blocks a
            # cross-site POST from reaching here, so this is defence in depth, not the gate.
            return _html(
                f"<h1>Could not revoke</h1><div class='msg err'>{html.escape(str(exc))}</div>",
                400,
            )
        logger.info("machine client %s revoked from the account page", client_id)
        return _redirect(f"{self._public}/account")

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
        "<main class=narrow>"
        "<p class=eyebrow>Authorize</p>"
        f"<h1 style='font-size:1.7rem'>Authorize {client_name}</h1>"
        "<div class=card>"
        f"<p style='margin-top:0'>Signed in as <code>{email}</code>.</p>"
        f"<p>{client_name} is requesting access to PSE Edge market data on your behalf. "
        "It will be able to call this server's tools using your account's quota.</p>"
        f"<form method=post action='/consent' style='margin:0'>"
        f"<input type=hidden name=flow_id value='{flow_id}'>"
        f"<input type=hidden name=csrf_token value='{csrf_token}'>"
        "<button type=submit>Allow</button></form>"
        "</div></main>"
    )


def _enroll_page_html() -> str:
    return """
<main class=narrow>
<p class=eyebrow>Sign up</p>
<h1 style='font-size:1.7rem'>Enroll a passkey</h1>
<div class=card>
<p style='margin-top:0'>Create a passkey to finish — a fingerprint, face or device
PIN.</p>
<div id=msg></div>
<button id=go>Create passkey</button>
</div>
</main>
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
<main class=narrow>
<p class=eyebrow>Welcome back</p>
<h1 style='font-size:1.7rem'>Sign in</h1>
<div class=card>
<p style='margin-top:0'>Use the passkey you enrolled.</p>
<div id=msg></div>
<button id=go>Sign in with a passkey</button>
</div>
<p class=muted>No account yet? <a href='/signup'>Create one</a>.</p>
</main>
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


def _machine_clients_panel(
    clients: list[dict[str, Any]], csrf_token: str, totals: dict[str, int] | None = None
) -> str:
    """Operator-only: list machine clients and provide create/revoke controls."""
    totals = totals or {}
    active = [c for c in clients if not c["revoked_at"]]
    rows = "".join(
        "<tr><td><code>{cid}</code></td><td>{name}</td>"
        "<td style='text-align:right'>{requests:,}</td>"
        "<td style='text-align:right'>"
        "<form method=post action='/account/machine-clients/revoke' style='margin:0'>"
        "<input type=hidden name=csrf_token value='{csrf}'>"
        "<input type=hidden name=client_id value='{cid}'>"
        "<button type=submit class=ghost>Revoke</button>"
        "</form></td></tr>".format(
            cid=html.escape(c["client_id"]),
            name=html.escape(c["client_name"]),
            requests=totals.get(c["client_id"], 0),
            csrf=csrf_token,
        )
        for c in active
    )
    table = (
        "<table><tr><th>client_id</th><th>Name</th>"
        "<th style='text-align:right'>Requests, 90d</th><th></th></tr>"
        f"{rows}</table>"
        if rows
        else "<p class=muted>No machine clients yet.</p>"
    )
    return f"""
<div class=sectionhead><h2>Machine clients</h2>
  <span class=aside>headless / agent access</span></div>
<p class=muted>A <code>client_id</code>/<code>client_secret</code> pair for an agent that
can't sign in through a browser — a LangGraph app, a scheduled job. It authenticates with
the <code>client_credentials</code> grant and gets its own quota. Give each agent its
own.</p>
<div class=card>{table}</div>
<form method=post action='/account/machine-clients'>
  <input type=hidden name=csrf_token value='{csrf_token}'>
  <label>Name<br><input name=name placeholder="langgraph-app" required></label><br>
  <button type=submit>+ Create machine client</button>
</form>
"""


def _machine_client_created_page(created: dict[str, str], public_url: str) -> str:
    """Shown once, with the secret. It is not recoverable — only revocable and reissuable."""
    cid = html.escape(created["client_id"])
    secret = html.escape(created["client_secret"])
    return f"""
<main class=narrow>
<p class=eyebrow>Settings</p>
<h1 style='font-size:1.7rem'>Machine client created
  <span class='chip warn'>shown once</span></h1>
<div class=card>
<p style='margin-top:0'><strong>Copy the secret now — it is shown only this once.</strong>
Only its hash is stored, so it cannot be retrieved later. If you lose it, revoke this
client and create another.</p>
<div class=row><span class=muted>client_id</span>
  <span><code>{cid}</code>
  <button type=button class=ghost data-v="{cid}"
    onclick="navigator.clipboard.writeText(this.dataset.v)">Copy</button></span></div>
<div class=row><span class=muted>client_secret</span>
  <span><code>{secret}</code>
  <button type=button class=ghost data-v="{secret}"
    onclick="navigator.clipboard.writeText(this.dataset.v)">Copy</button></span></div>
</div>
<h2>Use it</h2>
<p>Exchange the pair for a 1-hour bearer token (no refresh token — re-request when it
expires), then call the MCP endpoint with it:</p>
<pre>curl -X POST {public_url}/oauth/token \\
  -d grant_type=client_credentials \\
  -d client_id={cid} \\
  -d client_secret=&lt;the secret above&gt; \\
  -d scope=mcp -d resource={public_url}/mcp</pre>
<p><a class=btnlink href='/account#machine'>Back to your account</a></p>
</main>
"""


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _account_page(summary: AccountSummary, csrf_token: str, machine_panel: str = "") -> str:
    """The subject-access view, session control and the deletion control, as one tabbed
    page. Every tab is in the DOM (forms included) and a little script shows one at a
    time — without JavaScript the sections simply stack, nothing is unreachable."""
    tabs: list[tuple[str, str, str]] = [
        ("profile", "Profile", ""),
        ("usage", "Usage", ""),
        ("security", "Security", ""),
    ]
    if machine_panel:
        tabs.append(("machine", "Machine clients", ""))
    tabs.append(("danger", "Danger zone", "tab-danger"))
    tab_bar = "<nav class=tabs>" + "".join(
        f"<a href='#{tid}' class='{cls}'>{label}</a>" for tid, label, cls in tabs
    ) + "</nav>"

    usage_rows = "".join(
        f"<tr><td>{day['day']}</td><td>{day['requests']}</td><td>{day['rejected']}</td></tr>"
        for day in summary.usage_days[:30]
    )
    usage_table = (
        f"<table><tr><th>Day</th><th>Requests</th><th>Refused</th></tr>{usage_rows}</table>"
        if usage_rows
        else "<p class=muted>No usage recorded yet.</p>"
    )

    passkey_rows = "".join(
        "<div class=row><span><strong>Passkey</strong></span>"
        f"<span class=muted>Added {_fmt_date(pk['created_at'])}</span></div>"
        for pk in summary.passkey_list
    ) or "<p class=muted>No passkeys enrolled.</p>"

    session_rows = "".join(
        "<tr><td>{name}</td><td class=muted>{issued}</td>"
        "<td style='text-align:right'>"
        "<form method=post action='/account/sessions/revoke' style='margin:0'>"
        "<input type=hidden name=csrf_token value='{csrf}'>"
        "<input type=hidden name=family_id value='{fid}'>"
        "<button type=submit class=ghost>Revoke</button></form></td></tr>".format(
            name=html.escape(s["client_name"] or "CLI token"),
            issued=_fmt_date(s["created_at"]),
            csrf=csrf_token,
            fid=html.escape(s["family_id"] or ""),
        )
        for s in summary.sessions
    )
    sessions_table = (
        f"<table><tr><th>Client</th><th>Issued</th><th></th></tr>{session_rows}</table>"
        if session_rows
        else "<p class=muted>No connected clients right now.</p>"
    )

    machine_section = (
        f"<section class=tab id=machine>{machine_panel}</section>" if machine_panel else ""
    )

    return f"""
<main>
 <div class=pagehead>
  <div><p class=eyebrow>Settings</p><h1 style='margin-bottom:0'>Your account</h1></div>
  <div class=who>
    <div><code>{summary.email}</code></div>
    <div>Member since {_fmt_date(summary.created_at)} ·
         {_plural(summary.passkeys, "passkey")} ·
         {_plural(summary.active_tokens, "active token")}</div>
  </div>
 </div>
 {tab_bar}

 <section class=tab id=profile>
  <div class=sectionhead><h2>Identity</h2></div>
  <div class=card>
   <div class=row><span class=muted>Email</span>
     <span><code>{summary.email}</code> <span class=chip>&#10003; verified</span></span></div>
   <div class=row><span class=muted>Member since</span>
     <span>{_fmt_date(summary.created_at)}</span></div>
  </div>
 </section>

 <section class=tab id=usage>
  <div class=sectionhead><h2>Usage</h2><span class=aside>kept 90 days</span></div>
  <div class=card>{usage_table}</div>
 </section>

 <section class=tab id=security>
  <div class=sectionhead><h2>Passkeys</h2></div>
  <p class=muted>Passkeys are how you sign in. Keep at least two so losing a device
  doesn't lock you out.</p>
  <div class=card>{passkey_rows}</div>
  <p><a class=btnlink href='/enroll'>+ Add a passkey</a></p>
  <div class=sectionhead><h2>Sessions &amp; tokens</h2>
    <span class=aside>{_plural(summary.active_tokens, "active token")}</span></div>
  <p class=muted>One row per connected client. Revoking signs that client out within a
  minute. No IP addresses and no per-request activity are recorded — see the
  <a href='/privacy'>privacy page</a>.</p>
  <div class=card>{sessions_table}</div>
 </section>

 {machine_section}

 <section class=tab id=danger>
  <div class=sectionhead><h2>Danger zone</h2></div>
  <div class='card dangerzone'>
   <div class=row>
    <div class=grow>
     <strong>Delete your account</strong>
     <p class=muted style='margin:.3rem 0 0'>This removes your account, passkeys, tokens
     and usage history immediately and permanently. Any client still holding a token
     stops working within a minute. It cannot be undone — see the
     <a href='/privacy'>privacy page</a>.</p>
    </div>
    <form method=post action='/account/delete' style='margin:0'
          onsubmit="return confirm('Permanently delete your account? This cannot be undone.')">
     <input type=hidden name=csrf_token value='{csrf_token}'>
     <button type=submit class=danger>Delete account</button>
    </form>
   </div>
  </div>
 </section>
</main>
{_TABS_JS}
"""
