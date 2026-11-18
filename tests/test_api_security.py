"""The shared-secret API auth boundary (C1): fail closed.

Covers every rule from app.api.security:
- production + no secret configured -> 401 for everything (an internal
  network is never authentication)
- production + secret -> only the exact header value passes (constant-time
  compare; wrong or missing -> 401)
- development + no secret -> allowed (the documented dev bypass)
- development + secret set -> the secret is STILL required
- webhook secret and operator token are independent boundaries

Route-level stubs keep these tests on the auth layer only; HTTP mapping
lives in test_whatsapp_api.py. Rejected requests must never build a stack
(the dependency stops them before the route body runs).
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.whatsapp as whatsapp_api
from app.config import settings
from app.main import create_app
from app.messaging.processor import ProcessingOutcome

ANY_UUID = str(uuid4())


class _StubProcessor:
    def process(self, message) -> ProcessingOutcome:
        return ProcessingOutcome(status="processed", mark_read=True)


def _passthrough_stack() -> SimpleNamespace:
    return SimpleNamespace(
        processor=_StubProcessor(), outbox=None, conversations=None, handoff=None
    )


def _event() -> dict:
    return {
        "type": "notify",
        "messages": [
            {
                "key": {"remoteJid": "6281234567890@s.whatsapp.net", "id": "3A"},
                "message": {"conversation": "halo"},
            }
        ],
    }


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _no_stack_reached(monkeypatch):
    """Any stack build attempt on the webhook path means auth failed."""

    def boom():
        raise AssertionError("auth must reject before the stack is built")

    monkeypatch.setattr(whatsapp_api, "build_messaging_stack", boom)
    whatsapp_api.reset_messaging_stack()
    yield
    whatsapp_api.reset_messaging_stack()


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setattr(settings, "whatsapp_webhook_secret", "")
    monkeypatch.setattr(settings, "operator_api_token", "")


@pytest.fixture
def development(monkeypatch):
    monkeypatch.setattr(settings, "app_environment", "development")
    monkeypatch.setattr(settings, "whatsapp_webhook_secret", "")
    monkeypatch.setattr(settings, "operator_api_token", "")


# -- production: fail closed ---------------------------------------------


def test_production_without_secret_rejects_everything(client, production) -> None:
    assert client.post("/webhooks/whatsapp/events", json=_event()).status_code == 401
    assert (
        client.post(
            "/webhooks/whatsapp/delivery",
            json={"outbox_id": ANY_UUID, "status": "delivered"},
        ).status_code
        == 401
    )
    assert client.get("/conversations").status_code == 401
    assert client.post("/outbox/dispatch").status_code == 401


def test_production_wrong_or_missing_secret_is_401(
    client, production, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "whatsapp_webhook_secret", "hunter2")
    wrong = client.post(
        "/webhooks/whatsapp/events",
        json=_event(),
        headers={"X-Webhook-Secret": "hunter3"},
    )
    missing = client.post("/webhooks/whatsapp/events", json=_event())
    assert wrong.status_code == 401
    assert missing.status_code == 401


def test_production_correct_secret_passes(client, production, monkeypatch) -> None:
    monkeypatch.setattr(settings, "whatsapp_webhook_secret", "hunter2")
    monkeypatch.setattr(whatsapp_api, "build_messaging_stack", _passthrough_stack)
    response = client.post(
        "/webhooks/whatsapp/events",
        json=_event(),
        headers={"X-Webhook-Secret": "hunter2"},
    )
    assert response.status_code == 200


def test_production_operator_token_is_independent_boundary(
    client, production, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "operator_api_token", "op-token")
    # webhook secret unset: the webhook stays closed...
    assert client.post("/webhooks/whatsapp/events", json=_event()).status_code == 401
    # ...and operator routes need THEIR token, not the webhook secret
    with_webhook_secret = client.get(
        "/conversations", headers={"X-Webhook-Secret": "op-token"}
    )
    with_operator_token = client.get(
        "/conversations", headers={"X-Operator-Token": "op-token"}
    )
    assert with_webhook_secret.status_code == 401
    assert with_operator_token.status_code != 401


# -- development: the documented bypass ----------------------------------


def test_development_without_secret_allows_requests(
    client, development, monkeypatch
) -> None:
    monkeypatch.setattr(whatsapp_api, "build_messaging_stack", _passthrough_stack)
    response = client.post("/webhooks/whatsapp/events", json=_event())
    assert response.status_code == 200
    assert client.get("/conversations").status_code != 401


def test_development_with_secret_still_requires_it(
    client, development, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "operator_api_token", "op-token")
    assert client.get("/conversations").status_code == 401
    assert (
        client.get(
            "/conversations", headers={"X-Operator-Token": "op-token"}
        ).status_code
        != 401
    )
