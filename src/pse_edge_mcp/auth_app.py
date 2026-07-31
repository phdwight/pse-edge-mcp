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

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

from .email import EmailSender
from .oauth import (
    FatalAuthorizeError,
    OAuthError,
    OAuthService,
    RedirectAuthorizeError,
)
from .passkeys import SESSION_COOKIE, PasskeyError, PasskeyService, WebSession

Handler = Callable[[dict[str, Any], bytes], Awaitable[tuple[int, dict[str, str], bytes]]]

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
    ) -> None:
        self._app = app
        self._oauth = oauth
        self._passkeys = passkeys
        self._email = email
        self._public = public_url.rstrip("/")
        self._secure = self._public.startswith("https://")

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
            status, headers, payload = _json_response(exc.payload(), exc.status)
        except PasskeyError as exc:
            status, headers, payload = _json_response({"error": str(exc)}, 400)
        await _send(send, status, headers, payload)

    def _route(self, path: str, method: str) -> Handler | None:
        table: dict[tuple[str, str], Handler] = {
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
        }
        return table.get((path, method))

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
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],  # S256 only, never plain
                "token_endpoint_auth_methods_supported": ["none"],  # public clients
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
                return _html(_consent_page(flow.client_name, flow.flow_id, session.email or ""))
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
            return _html(_consent_page(flow.client_name, flow.flow_id, session.email or ""))
        return _redirect(f"{self._public}/login?flow={flow.flow_id}")

    async def _token(self, scope: dict[str, Any], body: bytes) -> tuple[int, dict[str, str], bytes]:
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        result = await self._oauth.exchange(form)
        # RFC 6749 §5.1: token responses must not be cached anywhere.
        return _json_response(result, headers={"cache-control": "no-store", "pragma": "no-cache"})

    async def _consent(
        self, scope: dict[str, Any], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if not session or session.kind != "authenticated" or not session.user_id:
            return _html("<h1>Session expired</h1><p>Start the authorization again.</p>", 403)
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
        await self._email.send(
            to=email.strip().lower(),
            subject="Verify your PSE Edge MCP account",
            html=(
                f'<p>Confirm this address to finish signing up:</p><p><a href="{link}">{link}</a>'
                "</p><p>The link expires in 30 minutes. If you did not request it, ignore "
                "this email.</p>"
            ),
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
        next_url = f"{self._public}/oauth/authorize?flow={flow_id}" if flow_id else self._public
        return _json_response({"ok": True, "next": next_url, "flow_id": flow_id})

    async def _require_session(self, scope: dict[str, Any]) -> WebSession:
        session = await self._passkeys.load_session(_cookies(scope).get(SESSION_COOKIE))
        if session is None:
            raise PasskeyError("your session expired — start again")
        return session


def _consent_page(client_name: str, flow_id: str, email: str) -> str:
    return (
        f"<h1>Authorize {client_name}</h1>"
        f"<p>Signed in as <code>{email}</code>.</p>"
        f"<p>{client_name} is requesting access to PSE Edge market data on your behalf. "
        "It will be able to call this server's tools using your account's quota.</p>"
        f"<form method=post action='/consent'>"
        f"<input type=hidden name=flow_id value='{flow_id}'>"
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
