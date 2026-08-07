"""Structured JSON logging (plan §6a).

One line of JSON per record, so a log shipper can index fields instead of regex-ing
prose. Off by default — a developer running the server in a terminal wants readable text,
and `PSE_LOG_JSON=1` turns it on for deployments.

Deliberately stdlib-only: `python-json-logger` and friends would be a runtime dependency
for something that is thirty lines, and invariant #5 says a dependency arrives only when
code genuinely needs it.

**Nothing here logs a credential.** Tokens, session ids and API keys never reach a log
line anywhere in this codebase; the redaction below is a backstop for anything that ends
up in a message by accident, not the primary control.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

# Anything shaped like one of our tokens, an Authorization header, or a ZeptoMail key.
# Each pattern captures the *label* so the redacted line still says which field was
# hidden. The generic one consumes to end of line on purpose: `Authorization: Bearer xyz`
# has the secret in the second word, and a `\S+` would have redacted only "Bearer".
# Over-redacting a log line is cheap; under-redacting one is not.
_SECRET_PATTERNS = (
    (re.compile(r"pse_[A-Za-z0-9_\-]{20,}"), "[redacted]"),
    (
        # The leading `[\w.-]*` is load-bearing: `\bsecret\b` does NOT match inside
        # `client_secret`, because `_` is a word character and so there is no boundary
        # before `secret`. That gap shipped, and it meant an OAuth client secret would
        # have been logged verbatim by anything that echoed a token-endpoint form.
        # Allowing a prefix catches client_secret, api-key, x_auth_token and friends.
        re.compile(
            r"(?i)([\w.-]*(?:authorization|api[-_]?key|apikey|token|password|secret))"
            r'"?\s*[:=]\s*.*'
        ),
        r"\1=[redacted]",
    ),
    (re.compile(r"(?i)(Zoho-enczapikey)\s+\S+"), r"\1 [redacted]"),
    # A Basic credential is base64(client_id:secret) — the secret is right there in the
    # blob, so the whole header value goes, not just the scheme.
    (re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}"), "Basic [redacted]"),
)

_STANDARD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        # Anything passed via `extra=` rides along as its own field, which is the point of
        # structured logging — no parsing the message to get at a user id.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    """Human-readable single line, timestamped, with the same redaction as the JSON one.

    Redaction has to be applied here too, not only in JsonFormatter: the plain format is
    what a developer sees in a terminal and what a deployment with `PSE_LOG_JSON` unset
    writes to its container log, so leaving it unredacted would mean the safer-looking
    format is the leakier one.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_HANDLER_NAME = "pse-edge-mcp"

# ISO-8601 with an explicit offset, matching JsonFormatter's `timestamp`. A log line with
# no time on it cannot be correlated with anything — not an incident, not a user's report,
# not the line above it after a restart — and this format sorts lexicographically.
_PLAIN_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(*, json_output: bool, level: str = "INFO") -> None:
    """Install the root handler. Idempotent, so repeated calls do not duplicate output."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if json_output else PlainFormatter(_PLAIN_FORMAT, datefmt=_TIME_FORMAT)
    )
    handler.set_name(_HANDLER_NAME)
    root = logging.getLogger()
    # Replace only handlers *we* installed. Clearing every root handler would make this
    # idempotent by stomping on whatever else is listening — pytest's capture, or a host
    # application that embedded this server — which is not ours to remove.
    for existing in list(root.handlers):
        if getattr(existing, "name", None) == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn installs its own handlers; let them propagate to ours so every line is
    # formatted the same way rather than half JSON and half uvicorn's prose.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
