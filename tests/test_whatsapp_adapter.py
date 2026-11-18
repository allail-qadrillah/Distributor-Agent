"""WhatsAppAdapter transport tests over httpx.MockTransport.

The safety rule under test is the outcome mapping:
- bridge confirms sent/failed -> sent/failed (retryable);
- connection never established -> failed;
- response lost after the request left (timeout, transport error,
  unparseable body) -> UNKNOWN, never auto-resent.
"""

import json
from uuid import uuid4

import httpx
import pytest

from app.whatsapp.adapter import WhatsAppAdapter

BRIDGE = "http://bridge.local"
OUTBOX_ID = uuid4()


def _adapter(handler, token: str = "secret") -> WhatsAppAdapter:
    return WhatsAppAdapter(
        bridge_url=BRIDGE, token=token, transport=httpx.MockTransport(handler)
    )


def test_sent_confirmed() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={"status": "sent"})

    outcome = _adapter(handler).send(OUTBOX_ID, "+6281234567890", "Stok 120 dus.")
    assert outcome.status == "sent"
    assert outcome.error is None
    body = json.loads(seen["body"])
    assert body == {
        "outbox_id": str(OUTBOX_ID),
        "to": "+6281234567890",
        "text": "Stok 120 dus.",
    }
    assert seen["auth"] == "Bearer secret"
    assert seen["path"] == "/messages"


def test_no_token_sends_no_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"status": "sent"})

    outcome = _adapter(handler, token="").send(OUTBOX_ID, "+6281234567890", "x")
    assert outcome.status == "sent"


def test_bridge_reported_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "failed", "error": "wa refused"})

    outcome = _adapter(handler).send(OUTBOX_ID, "+6281234567890", "x")
    assert outcome.status == "failed"
    assert outcome.error == "wa refused"


def test_http_5xx_is_unknown_never_resent() -> None:
    """H1: the bridge failed while processing; the request may already
    have reached WhatsApp — uncertain, never auto-resent."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    outcome = _adapter(handler).send(OUTBOX_ID, "+6281234567890", "x")
    assert outcome.status == "unknown"
    assert "503" in (outcome.error or "")


def test_http_4xx_is_retryable_failure() -> None:
    """4xx: the bridge refused the REQUEST itself — never handed to
    WhatsApp, confirmed-not-sent, safe to retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    outcome = _adapter(handler).send(OUTBOX_ID, "+6281234567890", "x")
    assert outcome.status == "failed"
    assert "400" in (outcome.error or "")


def test_connect_error_is_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    outcome = _adapter(handler).send(OUTBOX_ID, "+6281234567890", "x")
    assert outcome.status == "failed"
    assert "unreachable" in (outcome.error or "")


def test_read_timeout_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    outcome = _adapter(handler).send(OUTBOX_ID, "+6281234567890", "x")
    assert outcome.status == "unknown"
    assert outcome.error is not None


def test_malformed_2xx_body_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    assert _adapter(handler).send(OUTBOX_ID, "+6281234567890", "x").status == "unknown"


def test_unusable_json_body_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "maybe"})

    assert _adapter(handler).send(OUTBOX_ID, "+6281234567890", "x").status == "unknown"


def test_json_array_body_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["sent"])

    assert _adapter(handler).send(OUTBOX_ID, "+6281234567890", "x").status == "unknown"


def test_empty_bridge_url_rejected() -> None:
    with pytest.raises(ValueError, match="bridge_url"):
        WhatsAppAdapter(bridge_url="", token="secret")
