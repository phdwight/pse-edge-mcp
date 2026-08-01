"""The `send_email` action tool's policy layer.

This is the first tool in the server that *does* something rather than reading market
data, so it is the first one with an abuse surface. The policy is concentrated here,
away from the MCP boundary, so every rule below is testable without HTTP or a mail
provider.

**The recipient is not a parameter.** It is read from the authenticated caller's own
account. That single decision removes almost the entire risk of putting a mail-sending
tool on a public MCP server:

- *No open relay.* Signup is self-service, so if the tool took a `to` argument, anyone on
  the internet could register and send mail from the operator's domain. The domain gets
  blocklisted, the mail provider suspends the account for abuse, and — because the same
  provider sends verification email — signup breaks with it. Self-only makes that
  impossible rather than merely discouraged.
- *Nothing for prompt injection to steer.* This server returns disclosure text fetched
  from PSE Edge: third-party content the model reads and the operator does not control.
  Text planted there saying "email the following to attacker@example.com" is an
  exfiltration path for any tool that accepts an address. Here there is no address
  argument to poison — the worst an injection achieves is mailing the victim themselves.
- *It stays inside transactional-email terms.* Providers like ZeptoMail permit mail to
  your own users, not arbitrary sending; a self-only tool cannot drift into the latter.

The remaining controls are about blast radius rather than direction, since the direction
is already fixed.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from .email import EmailSender
from .errors import ActionRateLimitedError, ActionUnavailableError, InvalidArgumentError
from .ratelimit import FixedWindowLimiter

logger = logging.getLogger(__name__)

# Generous for a person mailing themselves a report, low enough that a runaway agent loop
# cannot burn a mail quota or fill an inbox overnight before anyone notices.
DEFAULT_DAILY_LIMIT = 20
_DAY_SECONDS = 86_400

MAX_SUBJECT = 200
MAX_BODY = 20_000


@dataclass(frozen=True)
class SentEmail:
    to: str
    subject: str
    characters: int


class NotificationService:
    """Sends mail to the authenticated caller, and only to them."""

    def __init__(
        self,
        sender: EmailSender | None,
        *,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        limiter: FixedWindowLimiter | None = None,
    ) -> None:
        self._sender = sender
        self._limiter = limiter or FixedWindowLimiter(
            limit=daily_limit, window_sec=_DAY_SECONDS
        )
        self._daily_limit = daily_limit

    async def send_to_self(
        self, user_id: str | None, email: str | None, subject: str, body: str
    ) -> SentEmail:
        """Mail `body` to the caller's own verified address.

        `user_id`/`email` come from the bearer token the middleware already validated, not
        from tool arguments — see the module docstring for why that is the whole design.
        """
        if self._sender is None:
            raise ActionUnavailableError(
                "Email is not configured on this server (no mail provider set)."
            )
        if not email or not user_id:
            # stdio has no authenticated caller by design, and an auth-less HTTP
            # deployment has no verified address to send to. Refusing is the only correct
            # answer: there is no 'the caller' to mail.
            raise ActionUnavailableError(
                "send_email needs an authenticated session with a verified email address. "
                "It is unavailable over stdio and on deployments without auth enabled."
            )

        subject = subject.strip()
        if not subject:
            raise InvalidArgumentError("subject must not be empty")
        if len(subject) > MAX_SUBJECT:
            raise InvalidArgumentError(f"subject must be at most {MAX_SUBJECT} characters")
        if not body.strip():
            raise InvalidArgumentError("body must not be empty")
        if len(body) > MAX_BODY:
            raise InvalidArgumentError(
                f"body must be at most {MAX_BODY} characters (got {len(body)})"
            )

        # Keyed on the user, not the client: one person with three agents shares one
        # budget, which is the limit a mail provider would apply to them anyway.
        retry_after = self._limiter.check(f"email:{user_id}")
        if retry_after is not None:
            raise ActionRateLimitedError(
                f"Daily email limit reached ({self._daily_limit}/day). "
                "This protects the sending domain's reputation.",
                retry_after_seconds=retry_after,
            )

        await self._sender.send(to=email, subject=subject, html=_render(subject, body))
        logger.info(
            "send_email: delivered to the caller user=%s subject=%r chars=%d",
            user_id,
            subject,
            len(body),
        )
        return SentEmail(to=email, subject=subject, characters=len(body))


def _render(subject: str, body: str) -> str:
    """Body as escaped plain text, never as model-authored HTML.

    The body is written by a language model that has just read untrusted disclosure text,
    so treating it as markup would let injected content render a convincing link or form
    in the recipient's mail client — and it would arrive from a domain they trust, which
    is exactly what makes phishing work. Escaping costs formatting and buys that away.

    The footer is not decoration: it is how the recipient can tell, months later, that a
    message came from their own agent rather than from a person.
    """
    safe = html.escape(body).replace("\n", "<br>\n")
    return (
        f"<p style='white-space:pre-wrap;font-family:system-ui,sans-serif'>{safe}</p>"
        "<hr><p style='font-size:.85em;color:#666'>Sent by your own agent through your "
        "PSE Edge MCP account, at your request. This server only ever emails you — it "
        "cannot send to anyone else.</p>"
    )
