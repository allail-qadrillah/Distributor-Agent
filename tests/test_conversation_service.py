"""ConversationService unit tests: persistence semantics, transcript
reconstruction, idempotency, state transitions. SQL-level guarantees are
covered by the integration tests; here the workflow is real, the
repositories and transaction are fakes."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.schemas import AgentInput, AgentMessage, AgentToolCall
from app.messaging.conversation_service import ConversationService
from app.messaging.exceptions import (
    ConversationNotFoundError,
    InvalidConversationStateError,
)
from app.messaging.identity import IdentityResolver
from tests.messaging_fakes import (
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeMessageRepository,
    FakeOutboxRepository,
    FakeTransaction,
    make_customer,
    patch_transaction,
)

PHONE = "+6281234567890"


@pytest.fixture
def tx(monkeypatch) -> FakeTransaction:
    fake = FakeTransaction()
    patch_transaction(monkeypatch, fake)
    return fake


@pytest.fixture
def service(tx) -> ConversationService:
    return ConversationService(
        FakeConversationRepository(),
        FakeMessageRepository(),
        FakeOutboxRepository(),
        IdentityResolver(FakeCustomerRepository()),
    )


def test_ingest_creates_conversation_and_claims_message(service) -> None:
    result = service.ingest_inbound(PHONE, "WA-1", "Stok Aqua?")
    assert result.conversation.phone == PHONE
    assert result.conversation.state == "active"
    assert result.message is not None
    assert result.message.status == "unread"
    assert result.message.role == "user"
    assert result.message.direction == "inbound"


def test_ingest_same_phone_reuses_conversation(service) -> None:
    first = service.ingest_inbound(PHONE, "WA-1", "pesan satu")
    second = service.ingest_inbound(PHONE, "WA-2", "pesan dua")
    assert first.conversation.id == second.conversation.id


def test_ingest_duplicate_external_id_returns_none(service) -> None:
    first = service.ingest_inbound(PHONE, "WA-1", "pesan")
    duplicate = service.ingest_inbound(PHONE, "WA-1", "pesan")
    assert first.message is not None
    assert duplicate.message is None


def test_ingest_unknown_sender_has_no_customer(service) -> None:
    result = service.ingest_inbound(PHONE, "WA-9", "halo")
    assert result.customer is None
    # the conversation row still exists (retention), no fake customer
    assert result.conversation.customer_id is None


def test_ingest_links_customer_lazily(tx) -> None:
    customers = FakeCustomerRepository()
    service = ConversationService(
        FakeConversationRepository(),
        FakeMessageRepository(),
        FakeOutboxRepository(),
        IdentityResolver(customers),
    )
    # first contact: unknown sender
    early = service.ingest_inbound(PHONE, "WA-1", "halo")
    assert early.customer is None
    # the number registers later
    customer = make_customer(PHONE)
    customers.by_phone[PHONE] = customer
    later = service.ingest_inbound(PHONE, "WA-2", "pesan")
    assert later.customer is not None
    assert later.conversation.customer_id == customer.id
    assert later.conversation.id == early.conversation.id  # same conversation


def test_intent_context_only_user_and_assistant_text(service) -> None:
    conversation = service.ingest_inbound(PHONE, "WA-1", "berapa harga?").conversation
    call = AgentToolCall(id="call-1", name="search_products", arguments="{}")
    service.persist_agent_run(
        conversation.id,
        [
            AgentMessage(role="assistant", tool_calls=[call]),
            AgentMessage(role="tool", tool_call_id="call-1", content='{"ok": true}'),
            AgentMessage(role="assistant", content="Stok 120 dus."),
        ],
    )
    latest = service.ingest_inbound(PHONE, "WA-2", "iya")
    assert latest.message is not None
    context = service.intent_context(
        conversation.id, before_seq=latest.message.seq, limit=10
    )
    assert ("user", "berapa harga?") in context
    assert ("assistant", "Stok 120 dus.") in context
    # tool protocol turns carry no text and never leak into intent context
    assert all(role in ("user", "assistant") for role, _ in context)


def test_load_transcript_reconstructs_agent_protocol(service) -> None:
    conversation = service.ingest_inbound(PHONE, "WA-1", "Stok Aqua?").conversation
    call = AgentToolCall(
        id="call-1", name="search_products", arguments='{"search_term": "Aqua"}'
    )
    service.persist_agent_run(
        conversation.id,
        [
            AgentMessage(role="assistant", tool_calls=[call]),
            AgentMessage(role="tool", tool_call_id="call-1", content='{"ok": true}'),
            AgentMessage(role="assistant", content="Stok 120 dus."),
        ],
    )
    transcript = service.load_transcript(conversation.id)
    assert [m.role for m in transcript] == ["user", "assistant", "tool", "assistant"]
    assert transcript[1].tool_calls[0].id == "call-1"
    assert transcript[2].tool_call_id == "call-1"
    # the reconstructed transcript is a valid replayable AgentInput
    # except the last message must be user: append one
    AgentInput(messages=[*transcript, AgentMessage(role="user", content="ok")])
    assert transcript[3].content == "Stok 120 dus."


def test_corrupt_tool_call_payload_fails_validation(tx) -> None:
    # the storage projection validates tool-call entries the same way the
    # protocol does: a blank id is rejected at the boundary (model_validate
    # of a corrupt database row fails here, never at the provider)
    from app.schemas.conversation import ToolCallPayload

    with pytest.raises(ValidationError):
        ToolCallPayload.model_validate(
            {"id": "  ", "name": "search_products", "arguments": "{}"}
        )


def test_persist_agent_run_maps_directions_and_creates_outbox(service) -> None:
    conversation = service.ingest_inbound(PHONE, "WA-1", "pesan").conversation
    call = AgentToolCall(id="call-1", name="search_products", arguments="{}")
    persisted = service.persist_agent_run(
        conversation.id,
        [
            AgentMessage(role="assistant", tool_calls=[call]),
            AgentMessage(role="tool", tool_call_id="call-1", content='{"ok": true}'),
            AgentMessage(role="assistant", content="Ringkasan pesanan: 10 dus."),
        ],
    )
    directions = [m.direction for m in persisted.messages]
    assert directions == ["agent", "agent", "outbound"]
    assert persisted.outbound is not None
    assert persisted.outbound.body == "Ringkasan pesanan: 10 dus."
    assert persisted.outbound.status == "pending"
    assert persisted.outbound.attempts == 0


def test_persist_agent_run_without_text_creates_no_outbox(service) -> None:
    conversation = service.ingest_inbound(PHONE, "WA-1", "pesan").conversation
    call = AgentToolCall(id="call-1", name="search_products", arguments="{}")
    persisted = service.persist_agent_run(
        conversation.id,
        [
            AgentMessage(role="assistant", tool_calls=[call]),
            AgentMessage(role="tool", tool_call_id="call-1", content='{"ok": true}'),
        ],
    )
    assert persisted.outbound is None


def test_persist_agent_run_rejects_user_role(service) -> None:
    conversation = service.ingest_inbound(PHONE, "WA-1", "pesan").conversation
    with pytest.raises(ValueError, match="inbound messages are claimed"):
        service.persist_agent_run(
            conversation.id, [AgentMessage(role="user", content="x")]
        )


def test_mark_message_read_once(service) -> None:
    result = service.ingest_inbound(PHONE, "WA-1", "pesan")
    assert result.message is not None
    assert service.mark_message_read(result.message.id) is True
    assert service.mark_message_read(result.message.id) is False  # guarded


def test_conversation_not_found(service) -> None:
    with pytest.raises(ConversationNotFoundError):
        service.get_conversation(uuid4())


def test_transition_state_valid_and_invalid(service) -> None:
    conversation = service.ingest_inbound(PHONE, "WA-1", "pesan").conversation
    updated = service.transition_state(
        conversation.id, ("active",), "needs_human", reason="gate:human_request"
    )
    assert updated.state == "needs_human"
    assert updated.handoff_reason == "gate:human_request"
    with pytest.raises(InvalidConversationStateError):
        service.transition_state(conversation.id, ("active",), "human_active")


def test_messages_listing_requires_conversation(service) -> None:
    with pytest.raises(ConversationNotFoundError):
        service.list_messages(uuid4())


def test_ingest_rolls_back_on_claim_failure(tx, monkeypatch) -> None:
    service = ConversationService(
        FakeConversationRepository(),
        FakeMessageRepository(),
        FakeOutboxRepository(),
        IdentityResolver(FakeCustomerRepository()),
    )
    conversation = service.ingest_inbound(PHONE, "WA-1", "pesan").conversation
    tx.journal.clear()

    def boom(conn, conversation_id, external_message_id, content):
        raise RuntimeError("db down")

    monkeypatch.setattr(service._messages, "claim_inbound", boom)
    with pytest.raises(RuntimeError):
        service.ingest_inbound(PHONE, "WA-2", "pesan dua")
    assert tx.journal == ["rollback"]
    assert conversation.id in service._conversations.rows  # earlier tx committed


# -- Phase 3 remediation regression tests ---------------------------------


def _full_service(tx, customers):
    """Service with the approval repository wired (H3 registration)."""
    from tests.messaging_fakes import FakeApprovalRepository

    return ConversationService(
        FakeConversationRepository(),
        FakeMessageRepository(),
        FakeOutboxRepository(),
        IdentityResolver(customers),
        FakeApprovalRepository(),
    )


def test_ingest_identity_mismatch_never_relinks(tx) -> None:
    """H4: the phone resolves to a DIFFERENT registered customer than the
    one the conversation is permanently linked to. The link is never
    overwritten, the message is stored, the mismatch is flagged."""
    customer = make_customer(PHONE)
    customers = FakeCustomerRepository({PHONE: customer})
    service = _full_service(tx, customers)
    first = service.ingest_inbound(PHONE, "WA-1", "pesan pertama")
    assert first.identity_mismatch is False
    assert first.conversation.customer_id == customer.id

    other = make_customer("+6281234567891")
    customers.by_phone[PHONE] = other  # number reassigned to another customer
    second = service.ingest_inbound(PHONE, "WA-2", "pesan kedua")
    assert second.identity_mismatch is True
    # the permanent link is untouched: the transcript belongs to customer
    assert second.conversation.customer_id == customer.id
    # the new message is still persisted (retention), never dropped
    assert second.message is not None and second.message.status == "unread"


def test_persist_agent_run_registers_approvals_atomically(tx) -> None:
    """H3: transcript rows, the outbox row, AND the pending_delivery
    approvals are written by ONE call — a sent summary can never exist
    without its approval row."""
    customer = make_customer(PHONE)
    service = _full_service(tx, FakeCustomerRepository({PHONE: customer}))
    conversation = service.ingest_inbound(PHONE, "WA-1", "pesan").conversation
    order_id = uuid4()
    run = service.persist_agent_run(
        conversation.id,
        [AgentMessage(role="assistant", content="ringkasan pesanan")],
        draft_order_ids=[order_id],
    )
    assert run.outbound is not None
    approvals = [r for r in service._approvals.rows.values() if r.order_id == order_id]
    assert len(approvals) == 1
    assert approvals[0].status == "pending_delivery"
    # bound to the exact delivery row (the column carries the outbox id,
    # matching ApprovalManager.register_drafts / activate_for_delivery)
    assert approvals[0].delivery_message_id == run.outbound.id


def test_persist_agent_run_without_reply_registers_no_approvals(tx) -> None:
    """H3 edge: drafts without a summary reply have no delivery to bind
    an approval to — nothing is registered."""
    customer = make_customer(PHONE)
    service = _full_service(tx, FakeCustomerRepository({PHONE: customer}))
    conversation = service.ingest_inbound(PHONE, "WA-1", "pesan").conversation
    run = service.persist_agent_run(
        conversation.id,
        [
            AgentMessage(
                role="assistant",
                tool_calls=[
                    AgentToolCall(id="call-x", name="search_products", arguments="{}")
                ],
            )
        ],
        draft_order_ids=[uuid4()],
    )
    assert run.outbound is None
    assert service._approvals.rows == {}


def test_load_agent_messages_bounds_window_and_pairs_tools(tx) -> None:
    """M5: the replay input holds only the newest N messages; a leading
    orphaned tool result (its assistant tool-call outside the window) is
    dropped so pairing stays valid. The full transcript stays persisted."""
    customer = make_customer(PHONE)
    service = _full_service(tx, FakeCustomerRepository({PHONE: customer}))
    conversation = service.ingest_inbound(PHONE, "WA-1", "tanya").conversation
    history = [
        AgentMessage(
            role="assistant",
            tool_calls=[
                AgentToolCall(id="call-1", name="search_products", arguments="{}")
            ],
        ),
        AgentMessage(
            role="tool",
            tool_call_id="call-1",
            content=json.dumps({"ok": True, "data": {}}),
        ),
    ]
    # the run slices off the input, so persist directly via full transcript:
    service.persist_agent_run(conversation.id, history)
    for i in range(3):
        service.ingest_inbound(PHONE, f"WA-{i + 2}", f"pesan {i}")

    assert len(service.load_transcript(conversation.id)) == 6  # untouched audit
    bounded = service.load_agent_messages(conversation.id, 4)
    # newest 4: the orphaned tool result fell out with its tool call
    assert [m.role for m in bounded] == ["user", "user", "user"]


def test_unread_inbound_lists_deferred_messages(tx) -> None:
    customer = make_customer(PHONE)
    service = _full_service(tx, FakeCustomerRepository({PHONE: customer}))
    conversation = service.ingest_inbound(PHONE, "WA-1", "pesan").conversation
    service.ingest_inbound(PHONE, "WA-2", "pesan dua")
    service.ingest_inbound(PHONE, "WA-3", "pesan tiga")
    rows = service.unread_inbound(conversation.id)
    assert [r.content for r in rows] == ["pesan", "pesan dua", "pesan tiga"]
