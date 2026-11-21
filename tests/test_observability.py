"""Unit tests for the application logging module: formatters, levels,
redaction, phone masking, and the correlation-id context.
"""

import io
import logging
from uuid import UUID

import pytest

from app import observability
from app.config import Settings
from app.observability import (
    ConsoleFormatter,
    JsonFormatter,
    configure_logging,
    log_event,
    mask_phone,
    normalize_correlation_id,
    set_correlation_id,
)


@pytest.fixture(autouse=True)
def _restore_root_logging():
    handlers = list(logging.getLogger().handlers)
    level = logging.getLogger().level
    yield
    root = logging.getLogger()
    root.handlers = handlers
    root.level = level


@pytest.fixture
def json_sink() -> io.StringIO:
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("app.test")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.propagate = False
    return sink


def _last_json(sink: io.StringIO) -> dict:
    line = sink.getvalue().strip().splitlines()[-1]
    import json

    return json.loads(line)


# -- log level configuration ---------------------------------------------


def test_default_log_level_is_info() -> None:
    configure_logging(stream=io.StringIO())
    assert logging.getLogger().level == logging.INFO


def test_configured_log_level_is_applied() -> None:
    configure_logging(level="WARNING", stream=io.StringIO())
    assert logging.getLogger().level == logging.WARNING


def test_log_level_from_settings_env_field() -> None:
    assert Settings(_env_file=None).log_level == "INFO"
    assert Settings(_env_file=None, log_level="DEBUG").log_level == "DEBUG"


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValueError):
        configure_logging(level="LOUD", stream=io.StringIO())


def test_configure_logging_is_idempotent() -> None:
    configure_logging(level="INFO", stream=io.StringIO())
    handler_count = len(logging.getLogger().handlers)
    configure_logging(level="DEBUG", stream=io.StringIO())
    assert len(logging.getLogger().handlers) == handler_count
    assert logging.getLogger().level == logging.DEBUG


# -- JSON formatter -------------------------------------------------------


def test_json_formatter_emits_structured_fields(json_sink) -> None:
    set_correlation_id("cid-json-1")
    try:
        log_event(
            logging.getLogger("app.test"),
            "intent_decision",
            category="order_related",
            confidence=0.9,
            duration_ms=42,
        )
    finally:
        observability.clear_correlation_id()
    entry = _last_json(json_sink)
    assert entry["event"] == "intent_decision"
    assert entry["level"] == "INFO"
    assert entry["service"] == "distributor-agent"
    assert entry["logger"] == "app.test"
    assert entry["correlation_id"] == "cid-json-1"
    assert entry["category"] == "order_related"
    assert entry["confidence"] == 0.9
    assert entry["duration_ms"] == 42
    assert "timestamp" in entry


def test_json_output_has_no_ansi_codes(json_sink) -> None:
    log_event(logging.getLogger("app.test"), "plain_event", value=1)
    assert "\x1b" not in json_sink.getvalue()


def test_json_formatter_survives_non_serializable_fields(json_sink) -> None:
    log_event(logging.getLogger("app.test"), "odd_event", weird=object())
    entry = _last_json(json_sink)
    assert entry["event"] == "odd_event"
    assert isinstance(entry["weird"], str)


def test_json_formatter_carries_exception_details(json_sink) -> None:
    try:
        raise RuntimeError("boom 6281234567890")
    except RuntimeError as exc:
        log_event(
            logging.getLogger("app.test"),
            "agent_run_failed",
            level=logging.ERROR,
            exc=exc,
            error_type=type(exc).__name__,
        )
    entry = _last_json(json_sink)
    assert entry["level"] == "ERROR"
    assert entry["error_type"] == "RuntimeError"
    assert entry["event"] == "agent_run_failed"
    assert "RuntimeError" in entry["traceback"]
    assert "6281234567890" not in entry["traceback"]


# -- console formatter ----------------------------------------------------


def test_console_formatter_is_human_readable_and_colored() -> None:
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(ConsoleFormatter(color=True))
    log = logging.getLogger("app.test.console")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.propagate = False
    set_correlation_id("cid-console-1")
    try:
        log_event(log, "intent_decision", category="order_related", duration_ms=7)
    finally:
        observability.clear_correlation_id()
    line = sink.getvalue()
    assert "intent_decision" in line
    assert "INFO" in line
    assert "cid-console-1" in line
    assert "category=order_related" in line
    assert "\x1b[" in line  # colored level


def test_console_formatter_without_color_has_no_ansi() -> None:
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(ConsoleFormatter(color=False))
    log = logging.getLogger("app.test.console2")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    log.propagate = False
    log_event(log, "plain", value=2)
    assert "\x1b" not in sink.getvalue()


# -- correlation id -------------------------------------------------------


def test_normalize_keeps_valid_correlation_id() -> None:
    assert normalize_correlation_id("bridge-abc-123") == "bridge-abc-123"


def test_normalize_generates_uuid_when_missing() -> None:
    value = normalize_correlation_id(None)
    UUID(value)  # parseable UUID
    assert value != ""


@pytest.mark.parametrize(
    "raw",
    [
        "with spaces",
        "with\nnewline",
        "x" * 65,
        "bad chars!?",
        "",
    ],
)
def test_normalize_replaces_invalid_correlation_id(raw: str) -> None:
    value = normalize_correlation_id(raw)
    UUID(value)


# -- redaction and masking ------------------------------------------------


def test_mask_phone_keeps_edges_only() -> None:
    masked = mask_phone("+6281234567890")
    assert "+6281234567890" not in masked
    assert masked.startswith("+628")
    assert masked.endswith("7890")
    assert "***" in masked


def test_mask_phone_leaves_short_digit_runs_alone() -> None:
    assert mask_phone("order 12345 for customer 12345678") == (
        "order 12345 for customer 12345678"
    )


def test_sensitive_keys_are_redacted(json_sink) -> None:
    log_event(
        logging.getLogger("app.test"),
        "provider_call",
        api_key="sk-secret",
        token="bearer-xyz",
        password="hunter2",
        authorization="Bearer abc",
        secret="wh0ok",
        nested={"db_url": "postgresql://u:p@h/db", "ok": 1},
    )
    entry = _last_json(json_sink)
    flat = str(entry)
    assert "sk-secret" not in flat
    assert "bearer-xyz" not in flat
    assert "hunter2" not in flat
    assert "Bearer abc" not in flat
    assert "wh0ok" not in flat
    assert "postgresql://u:p@h/db" not in flat
    assert entry["nested"]["ok"] == 1


def test_phone_numbers_masked_in_logged_strings(json_sink) -> None:
    log_event(
        logging.getLogger("app.test"),
        "identity_resolved",
        phone="+6281234567890",
        detail="sender 6289876543210 unknown",
    )
    entry = _last_json(json_sink)
    flat = str(entry)
    assert "+6281234567890" not in flat
    assert "6289876543210" not in flat
    assert "****" in flat or "***" in flat


def test_url_credentials_and_queries_stripped(json_sink) -> None:
    log_event(
        logging.getLogger("app.test"),
        "db_error",
        error="connect failed: postgresql://user:pw@host:5432/db?sslmode=require",
    )
    entry = _last_json(json_sink)
    assert "pw@" not in str(entry)
    assert "sslmode" not in str(entry)


def test_newlines_flattened_to_prevent_injection(json_sink) -> None:
    log_event(
        logging.getLogger("app.test"),
        "inbound",
        detail="line1\nline2\r\nline3",
    )
    entry = _last_json(json_sink)
    assert "line1 line2 line3" == entry["detail"]


def test_log_event_never_raises() -> None:
    class Boom:
        def __str__(self) -> str:
            raise ValueError("no")

    log_event(logging.getLogger("app.test"), "safe", weird=Boom())
    log_event(logging.getLogger("app.test"), "safe", level=logging.ERROR)
    log_event(None, "safe")  # type: ignore[arg-type]


def test_correlation_id_injected_into_every_event(json_sink) -> None:
    set_correlation_id("cid-auto-9")
    try:
        log_event(logging.getLogger("app.test"), "first")
        log_event(logging.getLogger("app.test"), "second")
    finally:
        observability.clear_correlation_id()
    lines = [ln for ln in json_sink.getvalue().strip().splitlines() if ln]
    import json

    entries = [json.loads(ln) for ln in lines]
    assert {e["correlation_id"] for e in entries} == {"cid-auto-9"}
    assert observability.current_correlation_id() is None
