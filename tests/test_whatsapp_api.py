"""API route tests for the Phase 3 surface: webhook events, delivery
reports, conversation operator routes, outbox dispatch.

Routes are tested with an injected fake MessagingStack (monkeypatched
composition root); workflow semantics are covered by the service tests,
these prove the HTTP mapping: 200 shapes, skip reasons surfaced to the
bridge, 404/409 state errors, 503 on construction failure.
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.conversations as conversations_api
import app.api.outbox as outbox_api
import app.api.whatsapp as whatsapp_api
from app.config import settings
from app.main import create_app
from app.messaging.conversation_service import ConversationService
from app.messaging.handoff import HandoffManager
from app.messaging.identity import IdentityResolver
from app.messaging.processor import ProcessingOutcome
from tests.messaging_fakes import (
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeMessageRepository,
    FakeOutboxRepository,
    FakeTransaction,
    null_lock,
    patch_transaction,
)

PHONE = "+6281234567890"


@pytest.fixture(autouse=True)
def _development_bypass(monkeypatch):
    """These tests target HTTP mapping, not the auth boundary (covered by
    test_api_security.py): the documented development bypass unblocks them."""
    monkeypatch.setattr(settings, "app_environment", "development")
    monkeypatch.setattr(settings, "whatsapp_webhook_secret", "")
    monkeypatch.setattr(settings, "operator_api_token", "")
    whatsapp_api.reset_messaging_stack()
    yield
    whatsapp_api.reset_messaging_stack()


def _baileys_event(text: str, jid: str = "6281234567890@s.whatsapp.net") -> dict:
    return {
        "type": "notify",
        "messages": [
            {
                "key": {"remoteJid": jid, "id": "3A", "fromMe": False},
                "message": {"conversation": text},
            }
        ],
    }


class StubProcessor:
    """Records processed messages; returns one scripted outcome."""

    def __init__(self, outcome: ProcessingOutcome) -> None:
        self.outcome = outcome
        self.seen: list = []

    def process(self, message) -> ProcessingOutcome:
        self.seen.append(message)
        return self.outcome


class StubOutbox:
    """Records delivery reports; returns one scripted reconciliation."""

    def __init__(self, row=None, activated=None) -> None:
        self.row = row
        self.activated = activated or []
        self.reports: list[tuple[UUID, bool]] = []

    def apply_delivery_report(self, outbox_id: UUID, delivered: bool):
        self.reports.append((outbox_id, delivered))
        return SimpleNamespace(
            row=self.row, activated=self.activated, activated_any=bool(self.activated)
        )

    def dispatch_pending(self, limit: int = 10) -> list:
        from app.messaging.outbox import SendReport

        return [SendReport(outbox_id=uuid4(), status="sent")]


def _stack(
    processor=None, outbox=None, conversations=None, handoff=None
) -> SimpleNamespace:
    return SimpleNamespace(
        processor=processor,
        outbox=outbox,
        conversations=conversations,
        handoff=handoff,
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


# -- webhooks/whatsapp/events -------------------------------------------


def test_events_processed_message_ack(client, monkeypatch) -> None:
    processor = StubProcessor(
        ProcessingOutcome(status="processed", mark_read=True, replied=True)
    )
    monkeypatch.setattr(
        whatsapp_api, "build_messaging_stack", lambda: _stack(processor=processor)
    )
    response = client.post(
        "/webhooks/whatsapp/events", json=_baileys_event("stok Aqua?")
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result == {
        "external_message_id": "3A",
        "status": "processed",
        "mark_read": True,
        "replied": True,
    }
    assert processor.seen[0].phone == PHONE
    assert processor.seen[0].text == "stok Aqua?"


def test_events_suppresses_read_receipt_for_handoff(client, monkeypatch) -> None:
    processor = StubProcessor(
        ProcessingOutcome(status="handoff", mark_read=False, conversation_id=uuid4())
    )
    monkeypatch.setattr(
        whatsapp_api, "build_messaging_stack", lambda: _stack(processor=processor)
    )
    response = client.post(
        "/webhooks/whatsapp/events", json=_baileys_event("saya mau ngobrol")
    )
    result = response.json()["results"][0]
    assert result["mark_read"] is False
    assert result["replied"] is False


def test_events_reports_skip_reasons(client, monkeypatch) -> None:
    monkeypatch.setattr(
        whatsapp_api,
        "build_messaging_stack",
        lambda: _stack(processor=StubProcessor(None)),
    )
    payload = {
        "type": "append",  # history backfill
        "messages": [
            {
                "key": {
                    "remoteJid": PHONE[1:] + "@s.whatsapp.net",
                    "id": "1",
                    "fromMe": False,
                }
            }
        ],
    }
    response = client.post("/webhooks/whatsapp/events", json=payload)
    assert response.json()["results"] == [
        {
            "external_message_id": None,
            "status": "skipped:history_backfill",
            "mark_read": False,
            "replied": False,
        }
    ]


def test_events_misconfigured_stack_is_503(client, monkeypatch) -> None:
    def boom():
        raise RuntimeError("missing credentials")

    monkeypatch.setattr(whatsapp_api, "build_messaging_stack", boom)
    response = client.post("/webhooks/whatsapp/events", json=_baileys_event("halo"))
    assert response.status_code == 503
    assert "service_misconfigured" in response.json()["detail"]


def test_events_stack_is_cached_not_rebuilt(client, monkeypatch) -> None:
    builds = []

    def build():
        builds.append(1)
        return _stack(
            processor=StubProcessor(
                ProcessingOutcome(status="processed", mark_read=True)
            )
        )

    monkeypatch.setattr(whatsapp_api, "build_messaging_stack", build)
    client.post("/webhooks/whatsapp/events", json=_baileys_event("satu"))
    client.post("/webhooks/whatsapp/events", json=_baileys_event("dua"))
    assert len(builds) == 1


# -- webhooks/whatsapp/delivery -----------------------------------------


def test_delivery_report_reconciles(client, monkeypatch) -> None:
    outbox = StubOutbox(row=SimpleNamespace(status="sent"))
    monkeypatch.setattr(
        whatsapp_api, "build_messaging_stack", lambda: _stack(outbox=outbox)
    )
    outbox_id = uuid4()
    response = client.post(
        "/webhooks/whatsapp/delivery",
        json={"outbox_id": str(outbox_id), "status": "delivered"},
    )
    assert response.status_code == 200
    assert response.json() == {"outbox_id": str(outbox_id), "status": "sent"}
    assert outbox.reports == [(outbox_id, True)]


def test_delivery_report_failed_status(client, monkeypatch) -> None:
    outbox = StubOutbox(row=SimpleNamespace(status="failed"))
    monkeypatch.setattr(
        whatsapp_api, "build_messaging_stack", lambda: _stack(outbox=outbox)
    )
    response = client.post(
        "/webhooks/whatsapp/delivery",
        json={"outbox_id": str(uuid4()), "status": "failed"},
    )
    assert response.json()["status"] == "failed"


def test_delivery_report_duplicate_is_noop(client, monkeypatch) -> None:
    outbox = StubOutbox(row=None)
    monkeypatch.setattr(
        whatsapp_api, "build_messaging_stack", lambda: _stack(outbox=outbox)
    )
    response = client.post(
        "/webhooks/whatsapp/delivery",
        json={"outbox_id": str(uuid4()), "status": "delivered"},
    )
    assert response.json()["status"] == "already_final"


def test_delivery_report_activation_reprocesses_deferred(client, monkeypatch) -> None:
    """M1: an approval that becomes active on a delivery report triggers
    reprocessing of the messages deferred behind it."""
    conversation_id = uuid4()
    row = SimpleNamespace(status="sent", conversation_id=conversation_id)
    approval = SimpleNamespace(order_id=uuid4())
    outbox = StubOutbox(row=row, activated=[approval])
    reprocessed: list[UUID] = []

    class StubReprocessor:
        def reprocess_deferred(self, conversation_id: UUID) -> list:
            reprocessed.append(conversation_id)
            return []

    monkeypatch.setattr(
        whatsapp_api,
        "build_messaging_stack",
        lambda: _stack(outbox=outbox, processor=StubReprocessor()),
    )
    response = client.post(
        "/webhooks/whatsapp/delivery",
        json={"outbox_id": str(uuid4()), "status": "delivered"},
    )
    assert response.status_code == 200
    assert reprocessed == [conversation_id]


def test_delivery_report_no_activation_no_reprocessing(client, monkeypatch) -> None:
    row = SimpleNamespace(status="failed", conversation_id=uuid4())
    outbox = StubOutbox(row=row)

    class ExplodingReprocessor:
        def reprocess_deferred(self, conversation_id: UUID) -> list:
            raise AssertionError("reprocessing must not run without activation")

    monkeypatch.setattr(
        whatsapp_api,
        "build_messaging_stack",
        lambda: _stack(outbox=outbox, processor=ExplodingReprocessor()),
    )
    response = client.post(
        "/webhooks/whatsapp/delivery",
        json={"outbox_id": str(uuid4()), "status": "failed"},
    )
    assert response.status_code == 200


def test_delivery_report_rejects_invalid_status(client) -> None:
    response = client.post(
        "/webhooks/whatsapp/delivery",
        json={"outbox_id": str(uuid4()), "status": "maybe"},
    )
    assert response.status_code == 422


# -- conversations ------------------------------------------------------


@pytest.fixture
def operator_stack(monkeypatch):
    """Real ConversationService + HandoffManager over fake repositories."""
    patch_transaction(monkeypatch, FakeTransaction())
    conversations_repo = FakeConversationRepository()
    customer = SimpleNamespace(id=uuid4())
    stack = SimpleNamespace(
        conversations=ConversationService(
            conversations_repo,
            FakeMessageRepository(),
            FakeOutboxRepository(),
            IdentityResolver(FakeCustomerRepository({PHONE: customer})),
        ),
        handoff=None,
    )
    stack.handoff = HandoffManager(stack.conversations, lock_factory=null_lock)
    monkeypatch.setattr(conversations_api, "build_messaging_stack", lambda: stack)
    return stack


def test_conversation_routes_list_and_detail(client, operator_stack) -> None:
    created = operator_stack.conversations.ingest_inbound(PHONE, "WA-0", "halo")
    conversation_id = created.conversation.id
    listing = client.get("/conversations")
    assert listing.status_code == 200
    assert [c["id"] for c in listing.json()["conversations"]] == [str(conversation_id)]
    detail = client.get(f"/conversations/{conversation_id}")
    assert detail.status_code == 200
    assert detail.json()["phone"] == PHONE
    messages = client.get(f"/conversations/{conversation_id}/messages")
    assert messages.status_code == 200
    assert [m["content"] for m in messages.json()["messages"]] == ["halo"]


def test_conversation_detail_unknown_is_404(client, operator_stack) -> None:
    assert client.get(f"/conversations/{uuid4()}").status_code == 404
    assert client.get(f"/conversations/{uuid4()}/messages").status_code == 404


def test_handoff_takeover_resume_lifecycle(client, operator_stack) -> None:
    conversation = operator_stack.conversations.ingest_inbound(
        PHONE, "WA-0", "halo"
    ).conversation
    base = f"/conversations/{conversation.id}"
    assert (
        client.post(f"{base}/handoff", json={"reason": "manual"}).json()["state"]
        == "needs_human"
    )
    assert client.post(f"{base}/takeover").json()["state"] == "human_active"
    assert client.post(f"{base}/resume").json()["state"] == "active"


def test_handoff_conflict_is_409(client, operator_stack) -> None:
    conversation = operator_stack.conversations.ingest_inbound(
        PHONE, "WA-0", "halo"
    ).conversation
    base = f"/conversations/{conversation.id}"
    client.post(f"{base}/handoff")
    response = client.post(f"{base}/takeover")
    response = client.post(f"{base}/takeover")  # already human_active
    assert response.status_code == 409
    assert "cannot go to" in response.json()["detail"]


def test_resume_active_is_409(client, operator_stack) -> None:
    conversation = operator_stack.conversations.ingest_inbound(
        PHONE, "WA-0", "halo"
    ).conversation
    response = client.post(f"/conversations/{conversation.id}/resume")
    assert response.status_code == 409


def test_handoff_unknown_conversation_is_404(client, operator_stack) -> None:
    assert client.post(f"/conversations/{uuid4()}/handoff").status_code == 404


def test_conversations_misconfigured_is_503(client, monkeypatch) -> None:
    def boom():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(conversations_api, "build_messaging_stack", boom)
    assert client.get("/conversations").status_code == 503


# -- outbox/dispatch ----------------------------------------------------


def test_dispatch_reports_results(client, monkeypatch) -> None:
    stack = _stack(outbox=StubOutbox())
    monkeypatch.setattr(outbox_api, "build_messaging_stack", lambda: stack)
    response = client.post("/outbox/dispatch")
    assert response.status_code == 200
    body = response.json()
    assert body["dispatched"] == 1
    assert body["results"][0]["status"] == "sent"


def test_dispatch_misconfigured_is_503(client, monkeypatch) -> None:
    def boom():
        raise RuntimeError("no bridge")

    monkeypatch.setattr(outbox_api, "build_messaging_stack", boom)
    assert client.post("/outbox/dispatch").status_code == 503
