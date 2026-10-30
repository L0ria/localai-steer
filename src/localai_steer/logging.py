"""Logging setup with API-key redaction (M0: key never printed, never logged in clear)."""

from __future__ import annotations

import logging
import re

DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

# Anything that looks like a bearer token or key value gets masked.
_REDACT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+"),
    re.compile(r"(?i)(x-localai-api-key\s*[:=]\s*)\S+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)\S+"),
)


def redact(text: str) -> str:
    """Mask credential-looking values in a log message."""
    out = text
    for pattern in _REDACT_PATTERNS:
        out = pattern.sub(r"\1[REDACTED]", out)
    return out


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.msg = redact(record.getMessage())
        record.args = ()
        return super().format(record)


def setup_logging(*, verbose: bool) -> None:
    """Configure the ``localai_steer`` logger (idempotent)."""
    level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger("localai_steer")
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_RedactingFormatter(DEFAULT_FORMAT, _DATE_FORMAT))
        root.addHandler(handler)
    for handler in root.handlers:
        handler.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"localai_steer.{name}")
