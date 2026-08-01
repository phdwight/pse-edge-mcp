"""Verification email delivery.

One protocol, two implementations: ZeptoMail (decided 2026-07-30 — the operator already
runs it) and a console sender for development and tests. The API key arrives via
environment only and is passed in here — this module never reads the environment itself,
so there is exactly one place configuration comes from.

Email is best-effort in the signup flow's eyes but failures are NOT swallowed: a user
who never receives their link experiences "signup is broken", so a send failure surfaces
to the caller loudly, unlike archive writes.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

ZEPTOMAIL_URL = "https://api.zeptomail.com/v1.1/email"


class EmailSendError(RuntimeError):
    """The provider refused or could not be reached.

    Its own type so the signup handler can tell "the mail provider is unhappy" from a
    genuine bug, and answer with something a person can act on instead of a 500.
    """


class EmailSender(Protocol):
    async def send(self, *, to: str, subject: str, html: str) -> None: ...


class ConsoleEmailSender:
    """Logs the email instead of sending it — the dev/test mode when no key is set."""

    async def send(self, *, to: str, subject: str, html: str) -> None:
        logger.info("email (console mode) to=%s subject=%r body=%s", to, subject, html)


class ZeptoMailSender:
    """ZeptoMail transactional send: one HTTPS POST with a Zoho-enczapikey header."""

    def __init__(
        self, api_key: str, from_address: str, http: httpx.AsyncClient | None = None
    ) -> None:
        self._from = from_address
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self._headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": f"Zoho-enczapikey {api_key}",
        }

    async def send(self, *, to: str, subject: str, html: str) -> None:
        payload = {
            "from": {"address": self._from},
            "to": [{"email_address": {"address": to}}],
            "subject": subject,
            "htmlbody": html,
        }
        response = await self._http.post(ZEPTOMAIL_URL, json=payload, headers=self._headers)
        if response.status_code < 400:
            # Logged because "did the email actually go out?" is the first question when a
            # user says they never got their link, and the answer is otherwise unknowable
            # from this side. The recipient is already in the database; the body is not
            # logged, and the API key never appears here.
            logger.info("email: sent to=%s subject=%r from=%s", to, subject, self._from)
        if response.status_code >= 400:
            # Loud: an unsendable verification link means signup is silently broken.
            #
            # The sender address is named because it is by far the most common cause, and
            # ZeptoMail often answers an unverified sender with a bare 500 and an empty
            # body — which says nothing at all unless the log says what was attempted.
            # ZeptoMail verifies exact domains, so `pse.example.com` is not covered by a
            # verified `example.com`.
            raise EmailSendError(
                f"ZeptoMail rejected the send ({response.status_code}) "
                f"from={self._from!r}: {response.text[:200] or '<empty body>'}"
            )


def build_email_sender(api_key: str | None, from_address: str) -> EmailSender:
    if api_key:
        return ZeptoMailSender(api_key, from_address)
    return ConsoleEmailSender()
