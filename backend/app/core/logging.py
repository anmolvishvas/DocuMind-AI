"""Structured JSON logging with per-request correlation IDs.

Importing this module has no side effects; call :func:`configure_logging`
explicitly at application startup.
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any, TextIO

CORRELATION_ID_HEADER = "X-Correlation-ID"
REDACTED = "[REDACTED]"

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Attributes present on every LogRecord; anything else came from ``extra=``.
_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "correlation_id",
    "taskName",
}

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
)


def generate_correlation_id() -> str:
    return uuid.uuid4().hex


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(correlation_id: str | None) -> Token[str | None]:
    """Bind a correlation ID to the current context; returns a reset token."""
    return _correlation_id.set(correlation_id)


def reset_correlation_id(token: Token[str | None]) -> None:
    _correlation_id.reset(token)


class CorrelationIdFilter(logging.Filter):
    """Attach the current correlation ID to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _sanitize(value: Any, key: str = "") -> Any:
    if key and _is_sensitive(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None) or get_correlation_id(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = _sanitize(value, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class _DocuMindHandler(logging.StreamHandler[TextIO]):
    """Marker subclass so repeated setup replaces only our own handler."""


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Configure the root logger to emit JSON. Safe to call repeatedly."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, _DocuMindHandler):
            root.removeHandler(existing)
    handler = _DocuMindHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationIdFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())
