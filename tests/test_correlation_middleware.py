"""Correlation-ID middleware behavior over the real FastAPI app factory:
header validation, generation, response echo, propagation into nested
application logs, and request lifecycle events.
"""

import io
import logging
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app
from app.observability import JsonFormatter, log_event


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _last_completed(caplog) -> logging.LogRecord:
    records = [r for r in caplog.records if r.msg == "http_request_completed"]
    assert records, "no http_request_completed event"
    return records[-1]


# -- header handling ------------------------------------------------------


def test_correlation_id_generated_when_missing(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    cid = response.headers["x-correlation-id"]
    UUID(cid)  # a fresh, parseable UUID


def test_correlation_id_replaced_when_invalid(client) -> None:
    response = client.get(
        "/health", headers={"X-Correlation-ID": "bad value\nwith junk"}
    )
    cid = response.headers["x-correlation-id"]
    UUID(cid)
    assert cid != "bad value\nwith junk"


def test_correlation_id_too_long_replaced(client) -> None:
    response = client.get("/health", headers={"X-Correlation-ID": "x" * 200})
    UUID(response.headers["x-correlation-id"])


def test_valid_correlation_id_preserved_and_echoed(client) -> None:
    response = client.get("/health", headers={"X-Correlation-ID": "bridge-flow-42"})
    assert response.headers["x-correlation-id"] == "bridge-flow-42"


# -- request lifecycle logs ------------------------------------------------


def test_request_completed_logged_with_status_and_duration(client, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.main")
    response = client.get("/health")
    assert response.status_code == 200
    record = _last_completed(caplog)
    assert record.evt["status"] == 200
    assert record.evt["method"] == "GET"
    assert record.evt["path"] == "/health"
    assert "duration_ms" in record.evt
    assert record.evt["outcome"] == "success"
    assert "correlation_id" in record.evt


def test_request_received_logged_before_processing(client, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="app.main")
    client.get("/health")
    received = [r for r in caplog.records if r.msg == "http_request_received"]
    assert received
    assert received[0].evt["path"] == "/health"


def test_no_request_bodies_or_query_strings_logged(client) -> None:
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        client.get("/health?token=should-not-appear")
    finally:
        root.removeHandler(handler)
    assert "should-not-appear" not in sink.getvalue()


# -- propagation into nested application logs ------------------------------


def test_correlation_id_reaches_nested_application_logs(
    client, monkeypatch, caplog
) -> None:
    import app.api.whatsapp as whatsapp_api
    from app.config import settings

    monkeypatch.setattr(settings, "app_environment", "development")
    monkeypatch.setattr(settings, "whatsapp_webhook_secret", "")
    whatsapp_api.reset_messaging_stack()

    class LoggingProcessor:
        def process(self, message):
            log_event(
                logging.getLogger("app.messaging.processor"),
                "nested_probe",
                status="processed",
            )
            from app.messaging.processor import ProcessingOutcome

            return ProcessingOutcome(status="processed", mark_read=True)

    monkeypatch.setattr(
        whatsapp_api, "build_messaging_stack", lambda: _fake_stack(LoggingProcessor())
    )
    caplog.set_level(logging.DEBUG)
    payload = {
        "type": "notify",
        "messages": [
            {
                "key": {
                    "remoteJid": "6281234567890@s.whatsapp.net",
                    "id": "3A",
                    "fromMe": False,
                },
                "message": {"conversation": "halo"},
            }
        ],
    }
    response = client.post(
        "/webhooks/whatsapp/events",
        json=payload,
        headers={"X-Correlation-ID": "trace-nested-7"},
    )
    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == "trace-nested-7"
    probe = [r for r in caplog.records if r.msg == "nested_probe"]
    assert probe
    assert probe[0].evt["correlation_id"] == "trace-nested-7"
    whatsapp_api.reset_messaging_stack()


def _fake_stack(processor) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(processor=processor, outbox=None)


# -- resilience ------------------------------------------------------------


def test_logging_failure_does_not_break_requests(client, monkeypatch) -> None:
    class BrokenFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            raise RuntimeError("formatter broken")

    from app.observability import AppLogHandler

    for handler in logging.getLogger().handlers:
        if isinstance(handler, AppLogHandler):
            monkeypatch.setattr(handler, "formatter", BrokenFormatter())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_creates_correlation_for_webhook_without_secret(
    client, monkeypatch, caplog
) -> None:
    """Development bypass posture: the middleware still stamps a fresh
    correlation id even when the auth boundary rejects the request."""
    caplog.set_level(logging.INFO, logger="app.main")
    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setattr(settings, "whatsapp_webhook_secret", "required-secret")
    response = client.post("/webhooks/whatsapp/events", json={})
    assert response.status_code == 401
    assert UUID(response.headers["x-correlation-id"])
    record = _last_completed(caplog)
    assert record.evt["status"] == 401
    assert record.evt["outcome"] == "client_error"
    assert record.levelno == logging.WARNING


def test_unhandled_error_still_echoes_correlation_and_logs(client, caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.main")

    @client.app.get("/explode")
    def explode() -> dict:
        raise RuntimeError("boom")

    response = client.get("/explode", headers={"X-Correlation-ID": "trace-boom-1"})
    assert response.status_code == 500
    assert response.headers["x-correlation-id"] == "trace-boom-1"
    record = _last_completed(caplog)
    assert record.evt["status"] == 500
    assert record.evt["outcome"] == "error"
    assert record.evt["error_type"] == "RuntimeError"
    assert record.levelno == logging.ERROR
