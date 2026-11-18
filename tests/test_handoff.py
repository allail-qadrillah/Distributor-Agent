"""HandoffManager state transitions: the agreed three states and their
exact moves. Everything else must raise."""

import pytest

from app.messaging.conversation_service import ConversationService
from app.messaging.exceptions import (
    ConversationNotFoundError,
    InvalidConversationStateError,
)
from app.messaging.handoff import HandoffManager
from app.messaging.identity import IdentityResolver
from tests.messaging_fakes import (
    FakeConversationRepository,
    FakeCustomerRepository,
    FakeMessageRepository,
    FakeOutboxRepository,
    FakeTransaction,
    patch_transaction,
)

PHONE = "+6281234567890"


@pytest.fixture
def stack(monkeypatch):
    patch_transaction(monkeypatch, FakeTransaction())
    conversations = ConversationService(
        FakeConversationRepository(),
        FakeMessageRepository(),
        FakeOutboxRepository(),
        IdentityResolver(FakeCustomerRepository()),
    )
    return conversations, HandoffManager(conversations)


def _conversation(conversations):
    return conversations.ingest_inbound(
        PHONE, f"WA-{uuid_counter()}", "pesan"
    ).conversation


def uuid_counter():
    global _counter
    _counter += 1
    return _counter


_counter = 0


def test_request_handoff_from_active(stack) -> None:
    conversations, handoff = stack
    conversation = _conversation(conversations)
    updated = handoff.request_handoff(conversation.id, reason="gate:human_request")
    assert updated.state == "needs_human"
    assert updated.handoff_reason == "gate:human_request"


def test_request_handoff_is_idempotent(stack) -> None:
    conversations, handoff = stack
    conversation = _conversation(conversations)
    first = handoff.request_handoff(conversation.id, reason="r1")
    second = handoff.request_handoff(conversation.id, reason="r2")
    assert first.state == second.state == "needs_human"
    assert second.handoff_reason == "r1"  # first reason kept


def test_takeover_from_needs_human(stack) -> None:
    conversations, handoff = stack
    conversation = _conversation(conversations)
    handoff.request_handoff(conversation.id)
    updated = handoff.takeover(conversation.id)
    assert updated.state == "human_active"


def test_takeover_requires_needs_human(stack) -> None:
    conversations, handoff = stack
    conversation = _conversation(conversations)  # active
    with pytest.raises(InvalidConversationStateError):
        handoff.takeover(conversation.id)


def test_resume_from_both_human_states(stack) -> None:
    conversations, handoff = stack
    conversation = _conversation(conversations)
    handoff.request_handoff(conversation.id)
    assert handoff.resume(conversation.id).state == "active"
    handoff.request_handoff(conversation.id)
    handoff.takeover(conversation.id)
    assert handoff.resume(conversation.id).state == "active"


def test_resume_requires_human_state(stack) -> None:
    conversations, handoff = stack
    conversation = _conversation(conversations)  # active
    with pytest.raises(InvalidConversationStateError):
        handoff.resume(conversation.id)


def test_unknown_conversation_raises(stack) -> None:
    from uuid import uuid4

    _, handoff = stack
    with pytest.raises(ConversationNotFoundError):
        handoff.resume(uuid4())


# -- Phase 3 remediation (H2): serialization with inbound processing ------


def test_operator_transitions_take_the_phone_lock(monkeypatch) -> None:
    """H2: operator state changes acquire the SAME per-phone advisory lock
    as inbound processing, so a handoff can never interleave with a
    running agent flow. The lock factory records what was locked."""
    patch_transaction(monkeypatch, FakeTransaction())
    conversations = ConversationService(
        FakeConversationRepository(),
        FakeMessageRepository(),
        FakeOutboxRepository(),
        IdentityResolver(FakeCustomerRepository()),
    )
    locked: list[str] = []

    from contextlib import contextmanager

    @contextmanager
    def recording_lock(phone):
        locked.append(phone)
        yield

    handoff = HandoffManager(conversations, lock_factory=recording_lock)
    conversation = _conversation(conversations)
    handoff.request_handoff(conversation.id, reason="gate")
    handoff.takeover(conversation.id)
    handoff.resume(conversation.id)
    # every operator transition locked the conversation's phone
    assert locked == [PHONE, PHONE, PHONE]


def test_acquire_lock_false_skips_reentrant_lock(monkeypatch) -> None:
    """The processor already holds the phone lock; its internal handoff
    calls must NOT try to take it again (pg_advisory_lock is
    session-scoped: a second attempt on a new connection self-deadlocks)."""
    patch_transaction(monkeypatch, FakeTransaction())
    conversations = ConversationService(
        FakeConversationRepository(),
        FakeMessageRepository(),
        FakeOutboxRepository(),
        IdentityResolver(FakeCustomerRepository()),
    )

    def exploding_lock(phone):
        raise AssertionError("must not re-acquire the phone lock")

    handoff = HandoffManager(conversations, lock_factory=exploding_lock)
    conversation = _conversation(conversations)
    updated = handoff.request_handoff(
        conversation.id, reason="identity_mismatch", acquire_lock=False
    )
    assert updated.state == "needs_human"
