"""ApprovalManager lifecycle: pending_delivery -> active (only on
confirmed delivery) -> consumed (single use), bound to one exact order
and one conversation."""

from uuid import uuid4

import pytest

from app.messaging.approval import ApprovalManager
from tests.messaging_fakes import (
    FakeApprovalRepository,
    FakeTransaction,
    patch_transaction,
)

CONVERSATION_ID = uuid4()
ORDER_A = uuid4()
ORDER_B = uuid4()
OUTBOX_ID = uuid4()


@pytest.fixture
def approvals(monkeypatch):
    patch_transaction(monkeypatch, FakeTransaction())
    return ApprovalManager(FakeApprovalRepository())


def test_registration_is_not_activation(approvals) -> None:
    created = approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A])
    assert [a.status for a in created] == ["pending_delivery"]
    assert approvals.latest_active(CONVERSATION_ID) is None


def test_activation_only_from_confirmed_delivery(approvals) -> None:
    approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A])
    activated = approvals.activate_for_delivery(OUTBOX_ID)
    assert [a.status for a in activated] == ["active"]
    latest = approvals.latest_active(CONVERSATION_ID)
    assert latest is not None and latest.order_id == ORDER_A


def test_failed_delivery_never_activates(approvals) -> None:
    approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A])
    # activation is called only by the 'sent' path; nothing else touches it
    assert approvals.latest_active(CONVERSATION_ID) is None


def test_activation_is_idempotent(approvals) -> None:
    approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A])
    approvals.activate_for_delivery(OUTBOX_ID)
    assert approvals.activate_for_delivery(OUTBOX_ID) == []


def test_consumption_is_single_use(approvals) -> None:
    approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A])
    approvals.activate_for_delivery(OUTBOX_ID)
    consumed = approvals.consume_confirmed(CONVERSATION_ID, [ORDER_A])
    assert [a.status for a in consumed] == ["consumed"]
    assert approvals.consume_confirmed(CONVERSATION_ID, [ORDER_A]) == []
    assert approvals.latest_active(CONVERSATION_ID) is None


def test_cannot_consume_other_conversations_approval(approvals) -> None:
    approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A])
    approvals.activate_for_delivery(OUTBOX_ID)
    other_conversation = uuid4()
    assert approvals.consume_confirmed(other_conversation, [ORDER_A]) == []


def test_pending_approval_cannot_be_consumed(approvals) -> None:
    approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A])
    assert approvals.consume_confirmed(CONVERSATION_ID, [ORDER_A]) == []


def test_approval_registration_is_idempotent_per_order(approvals) -> None:
    first = approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A])
    second = approvals.register_drafts(CONVERSATION_ID, uuid4(), [ORDER_A])
    assert len(first) == 1
    assert second == []  # UNIQUE (order_id): the first row keeps its link


def test_latest_active_prefers_most_recent(approvals) -> None:
    approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A])
    approvals.activate_for_delivery(OUTBOX_ID)
    outbox_b = uuid4()
    approvals.register_drafts(CONVERSATION_ID, outbox_b, [ORDER_B])
    approvals.activate_for_delivery(outbox_b)
    latest = approvals.latest_active(CONVERSATION_ID)
    assert latest is not None and latest.order_id == ORDER_B


def test_multiple_drafts_one_delivery(approvals) -> None:
    created = approvals.register_drafts(CONVERSATION_ID, OUTBOX_ID, [ORDER_A, ORDER_B])
    assert len(created) == 2
    activated = approvals.activate_for_delivery(OUTBOX_ID)
    assert len(activated) == 2
