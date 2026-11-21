"""Application observability: one logging configuration, structured
event emission, and the request-scoped correlation id.

Design contract (docs/DECISIONS.md, 2026-11-20):
- stdlib `logging` only, no third-party Python logging dependency;
- JSON lines in production or non-interactive output, colored console
  in development on a TTY, never ANSI inside JSON;
- every event carries timestamp/level/service/logger/event plus the
  correlation id from the request context (contextvars, never a
  global mutable);
- all field values pass through redaction: sensitive keys, phone
  numbers, URL credentials and query strings, control characters;
- `log_event` never raises: observability must not fail a business
  operation.
"""

import contextvars
import json
import logging
import re
import sys
import time
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

SERVICE = "distributor-agent"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "authorization",
        "auth",
        "credentials",
        "bearer",
        "webhook_secret",
        "operator_token",
        "db_url",
        "database_url",
        "supabase_db_url",
        "connection_string",
        "access_key",
        "private_key",
    }
)

_CORRELATION_MAX_LEN = 64
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PHONE_RE = re.compile(r"\+?\d{9,15}")
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^\s/@]+@)")
_URL_QUERY_RE = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s?]+)\?\S+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

# -- correlation context ---------------------------------------------------


def current_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str) -> None:
    _correlation_id.set(value)


def clear_correlation_id() -> None:
    _correlation_id.set(None)


def is_valid_correlation_id(value: str) -> bool:
    return bool(_CORRELATION_RE.fullmatch(value))


def new_correlation_id() -> str:
    return str(uuid4())


def normalize_correlation_id(raw: str | None) -> str:
    """The only path an external header value may enter a log line:
    reject anything that is not a short single-line token."""
    if raw is not None and is_valid_correlation_id(raw):
        return raw
    return new_correlation_id()


# -- redaction -------------------------------------------------------------


def mask_phone(text: str) -> str:
    return _PHONE_RE.sub(_mask_digits, text)


def _mask_digits(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits = raw.lstrip("+")
    prefix = raw[: len(raw) - len(digits)]
    return f"{prefix}{digits[:3]}***{digits[-4:]}"


def scrub_string(text: str) -> str:
    """Flatten control characters/newlines (log injection), mask phone
    numbers, and strip URL credentials and query strings."""
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = _URL_USERINFO_RE.sub(r"\1[redacted]@", text)
    text = _URL_QUERY_RE.sub(r"\1?[redacted]", text)
    return mask_phone(text)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_string(value)
    if isinstance(value, Mapping):
        return {
            key: (
                "[redacted]"
                if str(key).lower() in SENSITIVE_KEYS
                else redact_value(inner)
            )
            for key, inner in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(inner) for inner in value]
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {scrub_string(str(value))}"
    return value


# -- event emission ---------------------------------------------------------


def log_event(
    logger: logging.Logger | None,
    event: str,
    level: int = logging.INFO,
    exc: BaseException | None = None,
    **fields: Any,
) -> None:
    """Emit one structured event. Never raises: a logging failure must
    not fail the business operation it observes."""
    try:
        if logger is None:
            return
        payload = redact_value(fields)
        if isinstance(payload, dict):
            cid = current_correlation_id()
            if cid is not None and "correlation_id" not in payload:
                payload["correlation_id"] = cid
        exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
        logger.log(level, event, extra={"evt": payload}, exc_info=exc_info)
    except Exception:  # noqa: BLE001 - observability must never break flow
        pass


# -- formatters --------------------------------------------------------------


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat(
        timespec="milliseconds"
    )


def _fields(record: logging.LogRecord) -> dict:
    evt = getattr(record, "evt", None)
    return dict(evt) if isinstance(evt, dict) else {}


def _traceback_text(record: logging.LogRecord) -> str:
    if not record.exc_info:
        return ""
    return scrub_string("".join(traceback.format_exception(*record.exc_info)))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "service": SERVICE,
            "logger": record.name,
            # scrubbed: third-party messages (httpx URLs, DB errors) may
            # carry query strings, credentials, or phone numbers
            "event": scrub_string(record.getMessage()),
        }
        entry.update(_fields(record))
        if record.exc_info:
            entry.setdefault("error_type", record.exc_info[0].__name__)
            entry["traceback"] = _traceback_text(record)
        return json.dumps(entry, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    _COLORS = {
        "DEBUG": "\x1b[36m",
        "INFO": "\x1b[32m",
        "WARNING": "\x1b[33m",
        "ERROR": "\x1b[31m",
        "CRITICAL": "\x1b[35;1m",
    }
    _RESET = "\x1b[0m"

    def __init__(self, color: bool = True) -> None:
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        when = datetime.fromtimestamp(record.created, tz=UTC).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        level = record.levelname
        rendered = (
            f"{self._COLORS[level]}{level:<8}{self._RESET}"
            if self._color
            else f"{level:<8}"
        )
        parts = [
            f"{when} {rendered} [{record.name}] {scrub_string(record.getMessage())}"
        ]
        for key, value in _fields(record).items():
            parts.append(
                f"{key}={value if isinstance(value, str) else json.dumps(value, default=str)}"
            )
        line = " ".join(parts)
        tb = _traceback_text(record)
        if tb:
            line = f"{line}\n{tb}"
        return line


# -- configuration ------------------------------------------------------------


class AppLogHandler(logging.StreamHandler):
    """Marker class: idempotency anchor for configure_logging."""


def _configured_level(level: str | None) -> str:
    if level is None:
        from app.config import settings

        level = settings.log_level
    level_name = str(level).upper()
    if level_name not in VALID_LOG_LEVELS:
        raise ValueError(
            f"invalid LOG_LEVEL {level_name!r}; expected one of {VALID_LOG_LEVELS}"
        )
    return level_name


def _auto_json_mode(stream: Any) -> bool:
    from app.config import settings

    if settings.app_environment == "production":
        return True
    return not bool(getattr(stream, "isatty", lambda: False)())


def configure_logging(
    level: str | None = None,
    stream: Any | None = None,
    json_mode: bool | None = None,
) -> None:
    """Configure root logging once. JSON when production or
    non-interactive, colored console on a development TTY. A repeated
    call with no explicit level keeps the current root level (external
    level management, e.g. pytest's caplog, must not be stomped)."""
    level_name = _configured_level(level)
    root = logging.getLogger()
    handler = next((h for h in root.handlers if isinstance(h, AppLogHandler)), None)
    if handler is None:
        root.setLevel(level_name)
        target = stream if stream is not None else sys.stdout
        use_json = json_mode if json_mode is not None else _auto_json_mode(target)
        color = bool(getattr(target, "isatty", lambda: False)())
        handler = AppLogHandler(target)
        handler.setFormatter(
            JsonFormatter() if use_json else ConsoleFormatter(color=color)
        )
        root.addHandler(handler)
    elif level is not None:
        root.setLevel(level_name)
    elif stream is not None:
        handler.stream = stream


def monotonic_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
