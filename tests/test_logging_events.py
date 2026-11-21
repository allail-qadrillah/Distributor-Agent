"""Event coverage tests: the important business and technical decisions
of the inbound pipeline, agent execution, and outbox lifecycle must be
visible as structured log events, carrying the request correlation id,
without ever leaking message content, raw tool arguments, or phone
numbers.
"""

import logging
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import app.api.whatsapp as whatsapp_api
from app.agents.exceptions import AgentProviderError
from app.agents.orchestrator import AgentOrchestrator
from app.agents.schemas import AgentMessage, AgentResponse, AgentToolCall
from app.config import settings
from app.intent.exceptions import IntentClassifierTimeoutError
from app.intent.gate import IntentGate
from app.intent.schemas import IntentClassification
from app.main import create_app
from app.messaging.approval import ApprovalManager
from app.messaging.conversation_service import ConversationService
from app.messaging.handoff import HandoffManager
from app.messaging.identity import IdentityResolver
from app.messaging.outbox import OutboxProcessor
from app.messaging.processor import MessageProcessor
from app.observability import (
    clear_correlation_id,
    set_correlation_id,
)
from app.tools.context import ToolContext
from app.tools.results import ToolResult
from app.whatsapp.adapter import WhatsAppAdapter
from tests.messaging_fakes import (
    FakeApprovalRepository,
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeMessageRepository,
    FakeOrchestrator,
    FakeOutboxRepository,
    FakeTransaction,
    FakeWhatsAppAdapter,
    StubClassifier,
    inbound,
    make_customer,
    null_lock,
    patch_transaction,
)

PHONE = "+6281234567890"
CUSTOMER = make_customer(PHONE)
ORDER_ID = uuid4()


def build(monkeypatch, script, classifier=None, adapter_outcomes=None):
    patch_transaction(monkeypatch, FakeTransaction())
    conversations_repo = FakeConversationRepository()
    messages_repo = FakeMessageRepository()
    outbox_repo = FakeOutboxRepository()
    approval_repo = FakeApprovalRepository()
    customer_repo = FakeCustomerRepository({PHONE: CUSTOMER})
    conversations = ConversationService(
        conversations_repo,
        messages_repo,
        outbox_repo,
        IdentityResolver(customer_repo),
        approval_repo,
    )
    handoff = HandoffManager(conversations, lock_factory=null_lock)
    approvals = ApprovalManager(approval_repo)
    gate = IntentGate(classifier, 0.85) if classifier is not None else IntentGate(None)
    orchestrator = FakeOrchestrator(script)
    adapter = FakeWhatsAppAdapter(adapter_outcomes or ["sent"])
    outbox = OutboxProcessor(outbox_repo, conversations_repo, approvals, adapter)
    processor = MessageProcessor(
        conversations=conversations,
        gate=gate,
        orchestrator=orchestrator,
        handoff=handoff,
        approvals=approvals,
        outbox=outbox,
        lock_factory=null_lock,
    )
    return SimpleNamespace(
        processor=processor,
        conversations=conversations,
        approvals=approvals,
        orchestrator=orchestrator,
        adapter=adapter,
        approval_repo=approval_repo,
        outbox_repo=outbox_repo,
        messages_repo=messages_repo,
        customer_repo=customer_repo,
        conversation_repo=conversations_repo,
        outbox=outbox,
        classifier=classifier,
    )


@pytest.fixture(autouse=True)
def _capture_all_levels(caplog):
    caplog.set_level(logging.DEBUG)
    yield


@pytest.fixture(autouse=True)
def _clean_correlation():
    clear_correlation_id()
    yield
    clear_correlation_id()


def events(caplog, name: str) -> list[dict]:
    return [
        record.evt
        for record in caplog.records
        if record.msg == name and isinstance(getattr(record, "evt", None), dict)
    ]


def one_event(caplog, name: str) -> dict:
    found = events(caplog, name)
    assert found, f"missing event {name}"
    return found[-1]


# -- intent gate -----------------------------------------------------------


def test_intent_allow_decision_logged(monkeypatch, caplog) -> None:
    set_correlation_id("trace-intent-allow")
    stack = build(
        monkeypatch,
        [[AgentMessage(role="assistant", content="ok")]],
        classifier=StubClassifier(
            IntentClassification(
                category="order_related", confidence=0.99, reason="order"
            )
        ),
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "stok Aqua?"))
    assert outcome.status == "processed"
    evt = one_event(caplog, "intent_decision")
    assert evt["allow_agent"] is True
    assert evt["category"] == "order_related"
    assert evt["confidence"] == 0.99
    assert evt["threshold"] == 0.85
    assert evt["reason"] == "order_related"
    assert "duration_ms" in evt
    assert evt["correlation_id"] == "trace-intent-allow"
    assert (
        one_event(caplog, "agent_run_completed")["correlation_id"]
        == "trace-intent-allow"
    )


def test_intent_rejection_below_threshold_logged(monkeypatch, caplog) -> None:
    stack = build(
        monkeypatch,
        [],
        classifier=StubClassifier(
            IntentClassification(
                category="order_related", confidence=0.42, reason="order"
            )
        ),
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert outcome.status == "handoff"
    evt = one_event(caplog, "intent_decision")
    assert evt["allow_agent"] is False
    assert evt["reason"] == "order_related_below_threshold"
    assert (
        one_event(caplog, "handoff_requested")["reason"]
        == "order_related_below_threshold"
    )


def test_classifier_failure_logged(monkeypatch, caplog) -> None:
    stack = build(
        monkeypatch,
        [],
        classifier=StubClassifier(
            error=IntentClassifierTimeoutError("classifier timeout")
        ),
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert outcome.status == "handoff"
    evt = one_event(caplog, "intent_decision")
    assert evt["allow_agent"] is False
    assert "classifier_error" in evt["reason"]
    record = [r for r in caplog.records if r.msg == "intent_decision"][-1]
    assert record.levelno == logging.WARNING


def test_disabled_gate_logged_as_dev_bypass(monkeypatch, caplog) -> None:
    stack = build(monkeypatch, [[AgentMessage(role="assistant", content="ok")]])
    stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    evt = one_event(caplog, "intent_decision")
    assert evt["allow_agent"] is True
    assert evt["reason"] == "intent_gate_disabled"
    record = [r for r in caplog.records if r.msg == "intent_decision"][-1]
    assert record.levelno == logging.INFO


# -- identity and conversation ----------------------------------------------


def test_identity_resolution_masks_phone(monkeypatch, caplog) -> None:
    stack = build(monkeypatch, [[AgentMessage(role="assistant", content="ok")]])
    stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    evt = one_event(caplog, "identity_resolved")
    assert evt["customer_found"] is True
    assert evt["phone"] != PHONE
    assert evt["phone"].startswith("+628")
    assert evt["phone"].endswith("7890")
    assert "conversation_id" in evt
    assert "conversation_resolved" in [r.msg for r in caplog.records]


def test_unknown_sender_logged(monkeypatch, caplog) -> None:
    stack = build(monkeypatch, [])
    stack.customer_repo.by_phone = {}
    outcome = stack.processor.process(inbound(PHONE, "WA-9", "halo"))
    assert outcome.status == "unknown_sender"
    evt = one_event(caplog, "identity_resolved")
    assert evt["customer_found"] is False


def test_duplicate_inbound_logged(monkeypatch, caplog) -> None:
    stack = build(monkeypatch, [[AgentMessage(role="assistant", content="ok")]])
    stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    one_event(caplog, "duplicate_inbound")


def test_human_handling_logged(monkeypatch, caplog) -> None:
    stack = build(monkeypatch, [[AgentMessage(role="assistant", content="ok")]])
    stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    conversation = next(iter(stack.conversation_repo.rows.values()))
    stack.conversation_repo.set_state(None, conversation.id, ("active",), "needs_human")
    outcome = stack.processor.process(inbound(PHONE, "WA-2", "operator mode"))
    assert outcome.status == "human_handling"
    evt = one_event(caplog, "human_handling")
    assert evt["state"] == "needs_human"


def test_deferred_while_approval_pending_logged(monkeypatch, caplog) -> None:
    tool_call_turn = AgentMessage(
        role="assistant",
        tool_calls=[
            AgentToolCall(id="call-1", name="create_draft_order", arguments="{}")
        ],
    )
    draft = AgentMessage(
        role="tool",
        tool_call_id="call-1",
        content=(
            '{"tool_call_id":"call-1","tool_name":"create_draft_order",'
            f'"ok":true,"data":{{"order_id":"{ORDER_ID}","status":"draft"}}}}'
        ),
    )
    summary = AgentMessage(role="assistant", content="ringkasan order")
    stack = build(
        monkeypatch,
        [[tool_call_turn, draft, summary]],
        classifier=StubClassifier(
            IntentClassification(
                category="order_related", confidence=0.99, reason="order"
            )
        ),
        adapter_outcomes=["unknown"],  # delivery unconfirmed: approval stays pending
    )
    first = stack.processor.process(inbound(PHONE, "WA-1", "buat draft"))
    assert first.status == "processed"
    second = stack.processor.process(inbound(PHONE, "WA-2", "lanjut kirim"))
    assert second.status == "deferred"
    one_event(caplog, "inbound_deferred")
    approval = one_event(caplog, "approval_registered")
    assert approval["order_id"] == str(ORDER_ID)
    assert "outbox_id" in approval
    assert "outbox_id" in one_event(caplog, "outbox_message_created")


# -- agent execution ---------------------------------------------------------


def test_agent_failure_logged(monkeypatch, caplog) -> None:
    stack = build(
        monkeypatch,
        [AgentProviderError("provider unavailable")],
        classifier=StubClassifier(
            IntentClassification(
                category="order_related", confidence=0.99, reason="order"
            )
        ),
    )
    set_correlation_id("trace-agent-fail")
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert outcome.status == "agent_error"
    evt = one_event(caplog, "agent_run_failed")
    assert evt["error_type"] == "AgentProviderError"
    assert evt["correlation_id"] == "trace-agent-fail"
    record = [r for r in caplog.records if r.msg == "agent_run_failed"][-1]
    assert record.levelno == logging.ERROR
    assert one_event(caplog, "message_processed")["status"] == "agent_error"


def _text_response(text: str, response_id: str) -> AgentResponse:
    return AgentResponse(kind="text", text=text, response_id=response_id)


def _tool_response(response_id: str) -> AgentResponse:
    return AgentResponse(
        kind="tool_calls",
        response_id=response_id,
        tool_calls=[AgentToolCall(id="call-1", name="search_products", arguments="{}")],
    )


class StubProvider:
    def __init__(self, responses) -> None:
        self.responses = list(responses)

    def complete(self, messages, tools=None) -> AgentResponse:
        return self.responses.pop(0)


class StubExecutor:
    def __init__(self, result: ToolResult) -> None:
        self.result = result

    def openai_tools(self) -> list:
        return []

    def execute(self, tool_call_id, tool_name, arguments, context) -> ToolResult:
        return self.result


def test_provider_and_tool_events_logged(caplog) -> None:
    provider = StubProvider(
        [_tool_response("resp-1"), _text_response("selesai", "resp-2")]
    )
    executor = StubExecutor(
        ToolResult.failure("call-1", "search_products", "not_found", "tidak ada")
    )
    orchestrator = AgentOrchestrator(provider, executor)
    set_correlation_id("trace-tools-1")
    run = orchestrator.run(
        __import__("app.agents.schemas", fromlist=["AgentInput"]).AgentInput(
            messages=[AgentMessage(role="user", content="cari aqua")]
        ),
        ToolContext(customer_id=uuid4(), conversation_id="wa-42"),
    )
    assert run.final_text == "selesai"
    provider_evt = events(caplog, "provider_response_received")[0]
    assert provider_evt["outcome"] == "tool_calls"
    assert provider_evt["response_id"] == "resp-1"
    assert "duration_ms" in provider_evt
    started = one_event(caplog, "tool_execution_started")
    assert started["tool_name"] == "search_products"
    assert started["tool_call_id"] == "call-1"
    completed = one_event(caplog, "tool_execution_completed")
    assert completed["ok"] is False
    assert completed["category"] == "not_found"
    assert completed["correlation_id"] == "trace-tools-1"


def test_agent_run_events_use_request_correlation_id(caplog) -> None:
    provider = StubProvider([_text_response("ok", "resp-1")])
    orchestrator = AgentOrchestrator(provider, StubExecutor(None))
    set_correlation_id("trace-runevents-9")
    from app.agents.schemas import AgentInput

    run = orchestrator.run(
        AgentInput(messages=[AgentMessage(role="user", content="halo")]),
        ToolContext(customer_id=uuid4(), conversation_id="wa-42"),
    )
    clear_correlation_id()
    assert run.events[0].correlation_id == "trace-runevents-9"
    # no request context: the conversation id stays the fallback
    orchestrator2 = AgentOrchestrator(
        StubProvider([_text_response("ok", "r2")]), StubExecutor(None)
    )
    run2 = orchestrator2.run(
        AgentInput(messages=[AgentMessage(role="user", content="halo")]),
        ToolContext(customer_id=uuid4(), conversation_id="wa-42"),
    )
    assert run2.events[0].correlation_id == "wa-42"


def test_tool_events_do_not_leak_raw_arguments(caplog) -> None:
    provider = StubProvider([_tool_response("resp-1"), _text_response("ok", "resp-2")])
    secret_arguments = '{"customer_secret": "raw-arg-payload"}'
    executor = StubExecutor(
        ToolResult.failure("call-1", "search_products", "not_found", "-")
    )
    provider.responses[0].tool_calls[0].arguments = secret_arguments
    AgentOrchestrator(provider, executor).run(
        __import__("app.agents.schemas", fromlist=["AgentInput"]).AgentInput(
            messages=[AgentMessage(role="user", content="cari")]
        ),
        ToolContext(customer_id=uuid4(), conversation_id="wa-1"),
    )
    flat = str([getattr(r, "evt", None) for r in caplog.records])
    assert "raw-arg-payload" not in flat
    assert "cari" not in flat  # no customer message content either


# -- outbox dispatch and delivery --------------------------------------------


def _dispatch_stack(
    monkeypatch, outcome: str, max_attempts: int = 3
) -> OutboxProcessor:
    patch_transaction(monkeypatch, FakeTransaction())
    outbox_repo = FakeOutboxRepository()
    conversations_repo = FakeConversationRepository()
    conversation = conversations_repo.get_or_create_by_phone(None, PHONE)
    outbox_repo.create(None, uuid4(), conversation.id, "reply body")
    approvals = ApprovalManager(FakeApprovalRepository())
    adapter = FakeWhatsAppAdapter([outcome])
    return OutboxProcessor(
        outbox_repo, conversations_repo, approvals, adapter, max_attempts=max_attempts
    )


def test_dispatch_sent_logged(monkeypatch, caplog) -> None:
    set_correlation_id("trace-outbox-1")
    outbox = _dispatch_stack(monkeypatch, "sent")
    outbox.dispatch_pending()
    one_event(caplog, "outbox_message_claimed")
    one_event(caplog, "dispatch_started")
    evt = one_event(caplog, "dispatch_completed")
    assert evt["status"] == "sent"
    assert "outbox_id" in evt
    assert "duration_ms" in evt
    assert evt["correlation_id"] == "trace-outbox-1"


def test_dispatch_failed_logged(monkeypatch, caplog) -> None:
    outbox = _dispatch_stack(monkeypatch, "failed", max_attempts=1)
    outbox.dispatch_pending()
    evt = one_event(caplog, "dispatch_failed")
    assert evt["status"] == "failed"
    assert "outbox_id" in evt
    record = [r for r in caplog.records if r.msg == "dispatch_failed"][-1]
    assert record.levelno == logging.ERROR


def test_dispatch_unknown_logged_never_marked_retryable(monkeypatch, caplog) -> None:
    outbox = _dispatch_stack(monkeypatch, "unknown")
    reports = outbox.dispatch_pending()
    evt = one_event(caplog, "dispatch_outcome_unknown")
    assert evt["status"] == "unknown"
    assert "outbox_id" in evt
    # the 'unknown' outcome is never an automatic retry: one attempt only
    assert all(r.status == "unknown" for r in reports)


def test_delivery_report_reconciled_logged(monkeypatch, caplog) -> None:
    set_correlation_id("trace-delivery-1")
    patch_transaction(monkeypatch, FakeTransaction())
    outbox_repo = FakeOutboxRepository()
    conversations_repo = FakeConversationRepository()
    conversation = conversations_repo.get_or_create_by_phone(None, PHONE)
    row = outbox_repo.create(None, uuid4(), conversation.id, "reply")
    approvals = ApprovalManager(FakeApprovalRepository())
    processor = OutboxProcessor(outbox_repo, conversations_repo, approvals, None)
    processor.apply_delivery_report(row.id, delivered=True)
    evt = one_event(caplog, "delivery_report_reconciled")
    assert evt["status"] == "sent"
    assert evt["outbox_id"] == str(row.id)
    assert evt["correlation_id"] == "trace-delivery-1"


def test_stale_sending_reconciled_logged(monkeypatch, caplog) -> None:
    patch_transaction(monkeypatch, FakeTransaction())
    outbox_repo = FakeOutboxRepository()
    conversations_repo = FakeConversationRepository()
    conversation = conversations_repo.get_or_create_by_phone(None, PHONE)
    outbox_repo.create(None, uuid4(), conversation.id, "reply")
    outbox_repo.claim_next(None, 3)
    approvals = ApprovalManager(FakeApprovalRepository())
    processor = OutboxProcessor(
        outbox_repo, conversations_repo, approvals, None, stale_after_seconds=0.0
    )
    processor.reconcile_stale_sending()
    evt = one_event(caplog, "stale_sending_reconciled")
    assert "outbox_id" in evt
    record = [r for r in caplog.records if r.msg == "stale_sending_reconciled"][-1]
    assert record.levelno == logging.WARNING


def test_approval_activation_and_consumption_logged(monkeypatch, caplog) -> None:
    patch_transaction(monkeypatch, FakeTransaction())
    approval_repo = FakeApprovalRepository()
    approvals = ApprovalManager(approval_repo)
    conversation_id = uuid4()
    order_id = uuid4()
    approval_repo.create_pending(None, conversation_id, order_id, uuid4())
    row = next(iter(approval_repo.rows.values()))
    approvals.activate_for_delivery(row.delivery_message_id)
    evt = one_event(caplog, "approval_activated")
    assert evt["outbox_id"] == str(row.delivery_message_id)
    approvals.consume_confirmed(conversation_id, [order_id])
    consumed = one_event(caplog, "approval_consumed")
    assert consumed["conversation_id"] == str(conversation_id)


# -- adapter correlation propagation ------------------------------------------


def test_adapter_sends_correlation_header() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"status": "sent"})

    adapter = WhatsAppAdapter(
        bridge_url="http://bridge:3001",
        token="t",
        transport=httpx.MockTransport(handler),
    )
    set_correlation_id("trace-adapter-3")
    try:
        outcome = adapter.send(uuid4(), "6281234567890", "reply")
    finally:
        clear_correlation_id()
    assert outcome.status == "sent"
    assert captured["headers"]["x-correlation-id"] == "trace-adapter-3"


def test_adapter_omits_header_without_correlation() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"status": "sent"})

    adapter = WhatsAppAdapter(
        bridge_url="http://bridge:3001",
        token="t",
        transport=httpx.MockTransport(handler),
    )
    adapter.send(uuid4(), "6281234567890", "reply")
    assert "x-correlation-id" not in captured["headers"]


# -- webhook API events --------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _development_bypass(monkeypatch):
    monkeypatch.setattr(settings, "app_environment", "development")
    monkeypatch.setattr(settings, "whatsapp_webhook_secret", "")
    whatsapp_api.reset_messaging_stack()


def test_webhook_event_and_normalization_logged(client, monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _development_bypass(monkeypatch)

    class StubProcessor:
        def process(self, message):
            from app.messaging.processor import ProcessingOutcome

            return ProcessingOutcome(status="processed", mark_read=True)

    monkeypatch.setattr(
        whatsapp_api,
        "build_messaging_stack",
        lambda: SimpleNamespace(processor=StubProcessor(), outbox=None),
    )
    payload = {
        "type": "notify",
        "messages": [
            {
                "key": {
                    "remoteJid": "6281234567890@s.whatsapp.net",
                    "id": "3A",
                    "fromMe": False,
                },
                "message": {"conversation": "stok Aqua?"},
            }
        ],
    }
    response = client.post(
        "/webhooks/whatsapp/events",
        json=payload,
        headers={"X-Correlation-ID": "trace-webhook-1"},
    )
    assert response.status_code == 200
    evt = one_event(caplog, "webhook_event_received")
    assert evt["messages_found"] == 1
    assert evt["correlation_id"] == "trace-webhook-1"
    normalized = one_event(caplog, "webhook_message_normalized")
    assert normalized["external_message_id"] == "3A"
    flat = str([getattr(r, "evt", None) for r in caplog.records])
    assert "stok Aqua" not in flat  # no raw customer message content


def test_webhook_skip_reason_logged(client, monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _development_bypass(monkeypatch)
    monkeypatch.setattr(
        whatsapp_api,
        "build_messaging_stack",
        lambda: SimpleNamespace(processor=None, outbox=None),
    )
    payload = {"type": "append", "messages": [{"key": {"id": "X"}}]}
    response = client.post("/webhooks/whatsapp/events", json=payload)
    assert response.status_code == 200
    evt = one_event(caplog, "webhook_message_skipped")
    assert "skip_reason" in evt


def test_delivery_report_received_logged(client, monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    _development_bypass(monkeypatch)
    outbox_id = uuid4()

    class StubOutbox:
        def apply_delivery_report(self, outbox_id, delivered):
            from datetime import UTC, datetime

            from app.messaging.outbox import DeliveryReconciliation
            from app.schemas.conversation import OutboxMessageResponse

            row = OutboxMessageResponse(
                id=outbox_id,
                message_id=uuid4(),
                conversation_id=uuid4(),
                body="reply",
                status="sent",
                attempts=1,
                last_error=None,
                sent_at=datetime.now(UTC),
                claimed_at=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            return DeliveryReconciliation(row=row, activated=[])

    monkeypatch.setattr(
        whatsapp_api,
        "build_messaging_stack",
        lambda: SimpleNamespace(processor=None, outbox=StubOutbox()),
    )
    response = client.post(
        "/webhooks/whatsapp/delivery",
        json={"outbox_id": str(outbox_id), "status": "delivered"},
        headers={"X-Correlation-ID": "trace-delivery-2"},
    )
    assert response.status_code == 200
    evt = one_event(caplog, "delivery_report_received")
    assert evt["outbox_id"] == str(outbox_id)
    assert evt["delivered"] is True
    assert evt["correlation_id"] == "trace-delivery-2"
