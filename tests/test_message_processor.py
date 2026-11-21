"""MessageProcessor workflow tests: the Phase 3 inbound pipeline over
real application services with faked repositories, classifier, agent,
and transport.

Safety invariants under test (the Definition of Done list):
- duplicate external message ids never reprocess;
- unknown senders get no reply, no read receipt, no agent, no tools;
- needs_human / human_active block all automatic processing;
- every handoff category (and every classifier failure) stops BEFORE
  the agent, fail closed;
- agent runs replay the persisted transcript and get a trusted
  ToolContext (customer id, conversation id, the active approval);
- AgentError persists the partial transcript, sends nothing, retries
  nothing;
- drafts create approvals bound to the reply's delivery; confirmation
  consumes them; the full lifecycle holds.
"""

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.agents.exceptions import AgentProviderError
from app.agents.schemas import AgentMessage, AgentToolCall
from app.intent.exceptions import IntentClassifierTimeoutError
from app.intent.gate import IntentGate
from app.intent.schemas import IntentClassification
from app.messaging.approval import ApprovalManager
from app.messaging.conversation_service import ConversationService
from app.messaging.handoff import HandoffManager
from app.messaging.identity import IdentityResolver
from app.messaging.outbox import OutboxProcessor
from app.messaging.processor import MessageProcessor
from tests.messaging_fakes import (
    FakeApprovalRepository,
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeMessageRepository,
    FakeOrchestrator,
    FakeOutboxRepository,
    FakeTransaction,
    FakeWhatsAppAdapter,
    RecordingLock,
    StubClassifier,
    inbound,
    make_customer,
    null_lock,
    patch_transaction,
)

PHONE = "+6281234567890"
CUSTOMER = make_customer(PHONE)
ORDER_ID = uuid4()
OTHER_ORDER_ID = uuid4()


def _draft_result(order_id: UUID, status: str = "draft") -> AgentMessage:
    return AgentMessage(
        role="tool",
        tool_call_id="call-1",
        content=json.dumps(
            {
                "tool_call_id": "call-1",
                "tool_name": "create_draft_order",
                "ok": True,
                "data": {"order_id": str(order_id), "status": status},
            }
        ),
    )


def _confirm_result(order_id: UUID, ok: bool = True) -> AgentMessage:
    return AgentMessage(
        role="tool",
        tool_call_id="call-2",
        content=json.dumps(
            {
                "tool_call_id": "call-2",
                "tool_name": "confirm_order",
                "ok": ok,
                "data": {"order_id": str(order_id)},
            }
        ),
    )


def build(monkeypatch, script, classifier=None, adapter_outcomes=None):
    """Real messaging components over fakes; classifier=None disables the
    gate, StubClassifier instances script it."""
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
    lock = RecordingLock()
    processor = MessageProcessor(
        conversations=conversations,
        gate=gate,
        orchestrator=orchestrator,
        handoff=handoff,
        approvals=approvals,
        outbox=outbox,
        lock_factory=lock,
    )
    return SimpleNamespace(
        processor=processor,
        conversations=conversations,
        handoff=handoff,
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
        lock=lock,
    )


def test_duplicate_never_reprocesses(monkeypatch) -> None:
    stack = build(monkeypatch, [[AgentMessage(role="assistant", content="ok")]])
    first = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    second = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert first.status == "processed"
    assert second.status == "duplicate"
    assert second.mark_read is True
    assert len(stack.orchestrator.calls) == 1  # the agent ran exactly once


def test_duplicate_after_read_authorizes_read_receipt(monkeypatch) -> None:
    # the bridge retries a webhook when its HTTP response is lost; the
    # stored record (status 'read') authorizes the read receipt again
    stack = build(monkeypatch, [[AgentMessage(role="assistant", content="ok")]])
    first = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert first.status == "processed"
    assert first.mark_read is True
    stored = next(
        r for r in stack.messages_repo.rows.values() if r.external_message_id == "WA-1"
    )
    assert stored.status == "read"
    second = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert second.status == "duplicate"
    assert second.mark_read is True


def test_duplicate_unread_does_not_authorize_read_receipt(monkeypatch) -> None:
    failure = AgentProviderError("provider down")
    stack = build(monkeypatch, [failure])
    first = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert first.status == "agent_error"
    assert first.mark_read is False
    second = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert second.status == "duplicate"
    assert second.mark_read is False


def test_duplicate_unknown_sender_stays_unread(monkeypatch) -> None:
    stack = build(monkeypatch, [])
    phone = "+6289999999999"  # no registered customer
    first = stack.processor.process(inbound(phone, "WA-1", "halo admin"))
    assert first.status == "unknown_sender"
    assert first.mark_read is False
    second = stack.processor.process(inbound(phone, "WA-1", "halo admin"))
    assert second.status == "duplicate"
    assert second.mark_read is False


def test_duplicate_from_other_conversation_not_authorized(monkeypatch) -> None:
    stack = build(monkeypatch, [[AgentMessage(role="assistant", content="ok")]])
    first = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert first.status == "processed"
    other_phone = "+628111122222"
    stack.customer_repo.by_phone[other_phone] = make_customer(other_phone)
    cross = stack.processor.process(inbound(other_phone, "WA-1", "pesan"))
    assert cross.status == "duplicate"
    assert cross.mark_read is False
    assert cross.conversation_id != first.conversation_id


def test_unknown_sender_gets_nothing(monkeypatch) -> None:
    stack = build(monkeypatch, [])
    outcome = stack.processor.process(inbound("+628111122222", "WA-1", "halo admin"))
    assert outcome.status == "unknown_sender"
    assert outcome.mark_read is False
    assert outcome.replied is False
    assert stack.orchestrator.calls == []
    # the message is stored unread for later manual handling
    stored = stack.messages_repo.rows
    assert len(stored) == 1
    row = next(iter(stored.values()))
    assert row.status == "unread"
    assert row.direction == "inbound"


def test_needs_human_blocks_processing(monkeypatch) -> None:
    stack = build(monkeypatch, [])
    result = stack.conversations.ingest_inbound(PHONE, "WA-0", "pesan awal")
    stack.handoff.request_handoff(result.conversation.id, reason="manual")
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan saat handoff"))
    assert outcome.status == "human_handling"
    assert outcome.mark_read is False
    assert stack.orchestrator.calls == []
    stored = [
        r for r in stack.messages_repo.rows.values() if r.external_message_id == "WA-1"
    ]
    assert stored[0].status == "unread"


def test_human_active_blocks_processing(monkeypatch) -> None:
    stack = build(monkeypatch, [])
    result = stack.conversations.ingest_inbound(PHONE, "WA-0", "pesan awal")
    stack.handoff.request_handoff(result.conversation.id)
    stack.handoff.takeover(result.conversation.id)
    outcome = stack.processor.process(
        inbound(PHONE, "WA-1", "pesan saat ditangani manusia")
    )
    assert outcome.status == "human_handling"
    assert stack.orchestrator.calls == []


@pytest.mark.parametrize(
    ("category", "confidence"),
    [
        ("human_request", 0.99),
        ("out_of_scope", 0.99),
        ("ambiguous", 0.99),
        ("order_related", 0.5),  # below threshold
        ("order_related", 0.0),
    ],
)
def test_every_handoff_category_stops_before_the_agent(
    monkeypatch, category, confidence
) -> None:
    classifier = StubClassifier(
        IntentClassification(category=category, confidence=confidence, reason="test")
    )
    stack = build(monkeypatch, [], classifier=classifier)
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert outcome.status == "handoff"
    assert outcome.mark_read is False
    assert outcome.replied is False
    assert stack.orchestrator.calls == []  # the main agent was never invoked
    conversation = stack.conversations.get_conversation(outcome.conversation_id)
    assert conversation.state == "needs_human"
    stored = [
        r for r in stack.messages_repo.rows.values() if r.external_message_id == "WA-1"
    ]
    assert stored[0].status == "unread"


@pytest.mark.parametrize(
    "error",
    [
        IntentClassifierTimeoutError("timeout"),
        IntentClassifierTimeoutError("x"),
    ],
)
def test_classifier_failure_fails_closed(monkeypatch, error) -> None:
    classifier = StubClassifier(error=error)
    stack = build(monkeypatch, [], classifier=classifier)
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert outcome.status == "handoff"
    assert outcome.mark_read is False
    assert stack.orchestrator.calls == []


def test_classifier_provider_error_fails_closed(monkeypatch) -> None:
    from app.intent.exceptions import IntentClassifierProviderError

    classifier = StubClassifier(error=IntentClassifierProviderError("boom"))
    stack = build(monkeypatch, [], classifier=classifier)
    assert stack.processor.process(inbound(PHONE, "WA-1", "pesan")).status == "handoff"
    assert stack.orchestrator.calls == []


def test_invalid_output_fails_closed(monkeypatch) -> None:
    from app.intent.exceptions import IntentClassifierInvalidOutputError

    classifier = StubClassifier(error=IntentClassifierInvalidOutputError("garbage"))
    stack = build(monkeypatch, [], classifier=classifier)
    assert stack.processor.process(inbound(PHONE, "WA-1", "pesan")).status == "handoff"
    assert stack.orchestrator.calls == []


def test_allowed_message_runs_agent_and_replies(monkeypatch) -> None:
    classifier = StubClassifier(
        IntentClassification(category="order_related", confidence=0.95, reason="stok")
    )
    stack = build(
        monkeypatch,
        [[AgentMessage(role="assistant", content="Stok 120 dus.")]],
        classifier=classifier,
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "Stok Aqua?"))
    assert outcome.status == "processed"
    assert outcome.mark_read is True
    assert outcome.replied is True
    assert len(stack.orchestrator.calls) == 1
    agent_input, context = stack.orchestrator.calls[0]
    # trusted context: application identity, never model-provided
    assert context.customer_id == CUSTOMER.id
    assert context.conversation_id == str(outcome.conversation_id)
    assert context.approved_order_id is None
    assert agent_input.messages[-1].role == "user"
    assert agent_input.messages[-1].content == "Stok Aqua?"


def test_history_is_replayed_across_messages(monkeypatch) -> None:
    classifier = StubClassifier()
    stack = build(
        monkeypatch,
        [
            [AgentMessage(role="assistant", content="Stok 120 dus.")],
            [AgentMessage(role="assistant", content="Harga Rp150.")],
        ],
        classifier=classifier,
        adapter_outcomes=["sent", "sent"],
    )
    stack.processor.process(inbound(PHONE, "WA-1", "Stok Aqua?"))
    stack.processor.process(inbound(PHONE, "WA-2", "berapa harganya?"))
    second_input, _ = stack.orchestrator.calls[1]
    roles = [m.role for m in second_input.messages]
    assert roles == ["user", "assistant", "user"]
    assert second_input.messages[1].content == "Stok 120 dus."


def test_intent_context_excludes_current_message(monkeypatch) -> None:
    classifier = StubClassifier()
    stack = build(
        monkeypatch,
        [[AgentMessage(role="assistant", content="jawaban")]],
        classifier=classifier,
    )
    stack.processor.process(inbound(PHONE, "WA-1", "pertama"))
    stack.processor.process(inbound(PHONE, "WA-2", "kedua"))
    message, context = classifier.asked[-1]
    assert message == "kedua"
    # context holds the PRIOR turns only, never the message under test
    assert ("user", "pertama") in context
    assert all(text != "kedua" for _, text in context)


def test_disabled_gate_still_runs_agent(monkeypatch) -> None:
    stack = build(monkeypatch, [[AgentMessage(role="assistant", content="jawaban")]])
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan apa pun"))
    assert outcome.status == "processed"
    assert stack.orchestrator.calls != []


def test_agent_error_persists_partial_transcript_and_never_replies(monkeypatch) -> None:
    classifier = StubClassifier()
    error = AgentProviderError("provider down")
    error.new_messages = [
        AgentMessage(
            role="assistant",
            tool_calls=[
                AgentToolCall(id="call-1", name="search_products", arguments="{}")
            ],
        ),
        _draft_result(ORDER_ID),
    ]
    stack = build(
        monkeypatch, [error], classifier=classifier, adapter_outcomes=["sent"]
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan 10 dus"))
    assert outcome.status == "agent_error"
    assert outcome.mark_read is False  # M2: unsuccessful run, no read receipt
    assert outcome.replied is False
    assert outcome.detail == "AgentProviderError"
    transcript = stack.conversations.load_transcript(outcome.conversation_id)
    roles = [m.role for m in transcript]
    assert roles == ["user", "assistant", "tool"]  # partial preserved, no reply
    assert stack.adapter.sent == []  # nothing was sent
    assert len(stack.orchestrator.calls) == 1  # no retry
    # M2: the failed message stays unread (no read receipt via storage either)
    inbound_row = next(
        r for r in stack.messages_repo.rows.values() if r.direction == "inbound"
    )
    assert inbound_row.status == "unread"


def test_agent_error_before_any_work_persists_nothing(monkeypatch) -> None:
    classifier = StubClassifier()
    error = AgentProviderError("auth failed")  # no new_messages: no partials
    stack = build(monkeypatch, [error], classifier=classifier)
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert outcome.status == "agent_error"
    transcript = stack.conversations.load_transcript(outcome.conversation_id)
    assert [m.role for m in transcript] == ["user"]


def test_draft_creates_approval_bound_to_reply_delivery(monkeypatch) -> None:
    classifier = StubClassifier()
    script = [
        [
            AgentMessage(
                role="assistant",
                tool_calls=[
                    AgentToolCall(
                        id="call-1", name="create_draft_order", arguments="{}"
                    )
                ],
            ),
            _draft_result(ORDER_ID),
            AgentMessage(
                role="assistant", content="Ringkasan: 10 dus Aqua, total Rp1.500.000."
            ),
        ]
    ]
    stack = build(monkeypatch, script, classifier=classifier, adapter_outcomes=["sent"])
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan 10 dus"))
    assert outcome.status == "processed"
    conversation_id = outcome.conversation_id
    rows = list(stack.outbox_repo.rows.values())
    assert len(rows) == 1
    outbox_row = rows[0]
    registered = list(stack.approval_repo.rows.values())
    assert len(registered) == 1
    # registered as pending_delivery, bound to the reply's outbox row, then
    # activated only because the adapter confirmed delivery of the summary
    assert registered[0].order_id == ORDER_ID
    assert registered[0].delivery_message_id == outbox_row.id
    approval = stack.approvals.latest_active(conversation_id)
    assert approval is not None and approval.order_id == ORDER_ID


def test_failed_delivery_does_not_activate_approval(monkeypatch) -> None:
    classifier = StubClassifier()
    script = [
        [
            AgentMessage(
                role="assistant",
                tool_calls=[
                    AgentToolCall(
                        id="call-1", name="create_draft_order", arguments="{}"
                    )
                ],
            ),
            _draft_result(ORDER_ID),
            AgentMessage(role="assistant", content="Ringkasan."),
        ]
    ]
    stack = build(
        monkeypatch,
        script,
        classifier=classifier,
        adapter_outcomes=["failed", "failed", "failed"],
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan 10 dus"))
    assert outcome.replied is False
    assert stack.approvals.latest_active(outcome.conversation_id) is None
    # the approval exists but stays pending: no delivery, no activation
    registered = list(stack.approval_repo.rows.values())
    assert len(registered) == 1
    assert registered[0].status == "pending_delivery"


def test_unknown_delivery_does_not_activate_approval(monkeypatch) -> None:
    classifier = StubClassifier()
    script = [
        [
            AgentMessage(
                role="assistant",
                tool_calls=[
                    AgentToolCall(
                        id="call-1", name="create_draft_order", arguments="{}"
                    )
                ],
            ),
            _draft_result(ORDER_ID),
            AgentMessage(role="assistant", content="Ringkasan."),
        ]
    ]
    stack = build(
        monkeypatch, script, classifier=classifier, adapter_outcomes=["unknown"]
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan 10 dus"))
    assert outcome.replied is True  # attempted, unknown outcome
    assert stack.approvals.latest_active(outcome.conversation_id) is None


def test_full_lifecycle_confirm_consumes_approval(monkeypatch) -> None:
    classifier = StubClassifier()
    draft_run = [
        AgentMessage(
            role="assistant",
            tool_calls=[
                AgentToolCall(id="call-1", name="create_draft_order", arguments="{}")
            ],
        ),
        _draft_result(ORDER_ID),
        AgentMessage(role="assistant", content="Ringkasan pesanan."),
    ]
    confirm_run = [
        AgentMessage(
            role="assistant",
            tool_calls=[
                AgentToolCall(id="call-2", name="confirm_order", arguments="{}")
            ],
        ),
        _confirm_result(ORDER_ID),
        AgentMessage(role="assistant", content="Pesanan dikonfirmasi."),
    ]
    stack = build(
        monkeypatch,
        [draft_run, confirm_run],
        classifier=classifier,
        adapter_outcomes=["sent", "sent"],
    )
    first = stack.processor.process(inbound(PHONE, "WA-1", "pesan 10 dus"))
    approval = stack.approvals.latest_active(first.conversation_id)
    assert approval is not None

    second = stack.processor.process(inbound(PHONE, "WA-2", "ya, konfirmasi"))
    assert second.status == "processed"
    # the confirmation run saw the approved order injected in its context
    _, context = stack.orchestrator.calls[1]
    assert context.approved_order_id == ORDER_ID
    # and the approval was consumed (single-use)
    assert stack.approvals.latest_active(first.conversation_id) is None


def test_confirm_result_for_unapproved_order_consumes_nothing(monkeypatch) -> None:
    classifier = StubClassifier()
    confirm_run = [
        AgentMessage(
            role="assistant",
            tool_calls=[
                AgentToolCall(id="call-2", name="confirm_order", arguments="{}")
            ],
        ),
        _confirm_result(OTHER_ORDER_ID),
        AgentMessage(role="assistant", content="Pesanan dikonfirmasi."),
    ]
    stack = build(
        monkeypatch, [confirm_run], classifier=classifier, adapter_outcomes=["sent"]
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "konfirmasi pesanan lain"))
    assert stack.approvals.latest_active(outcome.conversation_id) is None


def test_lock_acquired_per_phone(monkeypatch) -> None:
    stack = build(monkeypatch, [[AgentMessage(role="assistant", content="ok")]])
    stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert stack.lock.acquired == [PHONE]


def test_blank_final_text_sends_nothing(monkeypatch) -> None:
    classifier = StubClassifier()
    # run produced only tool exchanges, no final text (empty transcript tail)
    stack = build(
        monkeypatch,
        [
            [
                AgentMessage(
                    role="assistant",
                    tool_calls=[
                        AgentToolCall(
                            id="call-1", name="search_products", arguments="{}"
                        )
                    ],
                ),
                AgentMessage(
                    role="tool", tool_call_id="call-1", content='{"ok": true}'
                ),
            ]
        ],
        classifier=classifier,
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "cek stok"))
    assert outcome.status == "processed"
    assert outcome.replied is False
    assert stack.adapter.sent == []


# -- Phase 3 remediation regression tests ---------------------------------


def test_identity_mismatch_stops_processing_and_hands_off(monkeypatch) -> None:
    """H4: the phone resolves to a different registered customer than the
    conversation's permanent link. No agent run under the mixed identity,
    no reply, no read receipt, no relinking — a human is flagged."""
    script = [
        [AgentMessage(role="assistant", content="jawaban")],
        [AgentMessage(role="assistant", content="harusnya tidak terkirim")],
    ]
    stack = build(monkeypatch, script)
    first = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert first.status == "processed"

    other = make_customer("+6281234567891")
    stack.customer_repo.by_phone[PHONE] = other  # number reassigned
    outcome = stack.processor.process(inbound(PHONE, "WA-2", "pesan kedua"))
    assert outcome.status == "identity_mismatch"
    assert outcome.mark_read is False
    assert outcome.replied is False
    # no second agent run: the transcript belongs to the LINKED customer
    assert len(stack.orchestrator.calls) == 1
    # human flagged, link untouched
    conversation = stack.conversations.get_conversation(first.conversation_id)
    assert conversation.state == "needs_human"
    assert conversation.handoff_reason == "identity_mismatch"
    assert conversation.customer_id == CUSTOMER.id


def test_reply_during_pending_delivery_is_deferred(monkeypatch) -> None:
    """M1: while the order summary awaits its delivery confirmation, a
    fast confirmation is NOT processed against a nonexistent approval.
    It stays unread and runs exactly once, after activation."""
    script = [
        [
            AgentMessage(
                role="assistant",
                tool_calls=[
                    AgentToolCall(
                        id="call-1", name="create_draft_order", arguments="{}"
                    )
                ],
            ),
            _draft_result(ORDER_ID),
            AgentMessage(role="assistant", content="ringkasan pesanan"),
        ],
        [
            AgentMessage(
                role="assistant",
                tool_calls=[
                    AgentToolCall(id="call-2", name="confirm_order", arguments="{}")
                ],
            ),
            _confirm_result(ORDER_ID),
            AgentMessage(role="assistant", content="pesanan dikonfirmasi"),
        ],
    ]
    # 'unknown' delivery: the approval stays pending_delivery
    stack = build(monkeypatch, script, adapter_outcomes=["unknown", "sent"])
    first = stack.processor.process(inbound(PHONE, "WA-1", "pesan 10 dus"))
    assert first.status == "processed"
    assert stack.approvals.pending_exists(first.conversation_id) is True

    deferred = stack.processor.process(inbound(PHONE, "WA-2", "ya, konfirmasi"))
    assert deferred.status == "deferred"
    assert deferred.mark_read is False
    assert len(stack.orchestrator.calls) == 1  # no run inside the window

    # delivery confirmed -> approval active -> deferred message runs once
    outbox_row = next(iter(stack.outbox_repo.rows.values()))
    stack.outbox.apply_delivery_report(outbox_row.id, delivered=True)
    assert stack.approvals.pending_exists(first.conversation_id) is False
    outcomes = stack.processor.reprocess_deferred(first.conversation_id)
    assert [o.status for o in outcomes] == ["processed"]
    assert len(stack.orchestrator.calls) == 2
    # the confirmation consumed the approval exactly once
    approval_row = next(
        r for r in stack.approval_repo.rows.values() if r.order_id == ORDER_ID
    )
    assert approval_row.status == "consumed"

    # reprocessing again is a no-op: the message is read, never re-run
    assert stack.processor.reprocess_deferred(first.conversation_id) == []
    assert len(stack.orchestrator.calls) == 2


def test_state_recheck_before_dispatch_keeps_reply_pending(monkeypatch) -> None:
    """H2 defense in depth: an operator handoff that lands between the
    agent run and dispatch leaves the reply PENDING in the outbox (never
    sent while the conversation is not active)."""
    stack = build(
        monkeypatch,
        [[AgentMessage(role="assistant", content="jawaban")]],
        adapter_outcomes=["sent"],
    )

    real_mark_read = stack.conversations.mark_message_read

    def mark_read_then_operator_takes_over(message_id):
        result = real_mark_read(message_id)
        # operator transition lands after the run, before dispatch
        conversation_id = stack.messages_repo.rows[message_id].conversation_id
        stack.conversations.transition_state(
            conversation_id, ("active",), "needs_human"
        )
        return result

    monkeypatch.setattr(
        stack.conversations, "mark_message_read", mark_read_then_operator_takes_over
    )
    outcome = stack.processor.process(inbound(PHONE, "WA-1", "pesan"))
    assert outcome.status == "processed"
    assert outcome.replied is False
    assert outcome.detail == "handoff_before_dispatch"
    assert stack.adapter.sent == []  # nothing left the system
    row = next(iter(stack.outbox_repo.rows.values()))
    assert row.status == "pending"  # waits for the operator
