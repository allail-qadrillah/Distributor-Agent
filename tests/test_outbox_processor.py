"""OutboxProcessor: dispatch flow, delivery states, unknown-outcome
safety, approval activation on confirmed delivery only."""

from uuid import uuid4

import pytest

from app.messaging.approval import ApprovalManager
from app.messaging.outbox import OutboxProcessor
from tests.messaging_fakes import (
    FakeApprovalRepository,
    FakeConversationRepository,
    FakeOutboxRepository,
    FakeTransaction,
    FakeWhatsAppAdapter,
    patch_transaction,
)

PHONE = "+6281234567890"
OTHER_PHONE = "+6281299999999"
ORDER_ID = uuid4()


@pytest.fixture
def parts(monkeypatch):
    patch_transaction(monkeypatch, FakeTransaction())
    outbox = FakeOutboxRepository()
    conversations = FakeConversationRepository()
    approvals = ApprovalManager(FakeApprovalRepository())
    return outbox, conversations, approvals


def _enqueue(parts, conversation_id, body="Ringkasan pesanan."):
    outbox, _, _ = parts
    return outbox.create(None, uuid4(), conversation_id, body)


def _conversation(parts, phone=PHONE):
    _, conversations, _ = parts
    return conversations.get_or_create_by_phone(None, phone)


def test_adapter_none_means_nothing_is_sent(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    processor = OutboxProcessor(outbox, conversations, approvals, adapter=None)
    assert processor.dispatch_conversation(conversation.id) == []
    assert outbox.rows[row.id].status == "pending"  # untouched


def test_sent_delivery_activates_approval(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    approvals.register_drafts(conversation.id, row.id, [ORDER_ID])
    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter(["sent"])
    )
    reports = processor.dispatch_conversation(conversation.id)
    assert [r.status for r in reports] == ["sent"]
    assert outbox.rows[row.id].status == "sent"
    assert outbox.rows[row.id].attempts == 1
    latest = approvals.latest_active(conversation.id)
    assert latest is not None and latest.order_id == ORDER_ID


def test_failed_delivery_does_not_activate(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    approvals.register_drafts(conversation.id, row.id, [ORDER_ID])
    processor = OutboxProcessor(
        outbox,
        conversations,
        approvals,
        adapter=FakeWhatsAppAdapter(["failed"]),
        max_attempts=1,
    )
    reports = processor.dispatch_conversation(conversation.id)
    assert [r.status for r in reports] == ["failed"]
    assert outbox.rows[row.id].status == "failed"
    assert approvals.latest_active(conversation.id) is None


def test_failed_is_retried_within_budget(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    # one dispatch drains until non-claimable: failed attempts are retried
    # inline within the attempt budget, then the message goes out
    adapter = FakeWhatsAppAdapter(["failed", "failed", "sent"])
    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=adapter, max_attempts=3
    )
    reports = processor.dispatch_conversation(conversation.id)
    assert [r.status for r in reports] == ["failed", "failed", "sent"]
    assert outbox.rows[row.id].status == "sent"
    assert outbox.rows[row.id].attempts == 3
    assert len(adapter.sent) == 3  # same body three times, no business re-execution


def test_failed_is_terminal_beyond_budget(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    processor = OutboxProcessor(
        outbox,
        conversations,
        approvals,
        adapter=FakeWhatsAppAdapter(["failed", "failed", "failed"]),
        max_attempts=2,
    )
    processor.dispatch_conversation(conversation.id)
    processor.dispatch_conversation(conversation.id)
    third = processor.dispatch_conversation(conversation.id)
    assert third == []  # attempts 2 == budget: no third claim
    assert outbox.rows[row.id].status == "failed"


def test_unknown_is_never_resend(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    approvals.register_drafts(conversation.id, row.id, [ORDER_ID])
    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter(["unknown"])
    )
    reports = processor.dispatch_conversation(conversation.id)
    assert [r.status for r in reports] == ["unknown"]
    assert outbox.rows[row.id].status == "unknown"
    # no automatic resend, no approval activation
    assert processor.dispatch_conversation(conversation.id) == []
    assert approvals.latest_active(conversation.id) is None


def test_unknown_reconciled_by_delivered_report(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    approvals.register_drafts(conversation.id, row.id, [ORDER_ID])
    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter(["unknown"])
    )
    processor.dispatch_conversation(conversation.id)
    reconciled = processor.apply_delivery_report(row.id, delivered=True)
    assert reconciled.row is not None and reconciled.row.status == "sent"
    # H3: the activation happened with the transition, same reconciliation
    assert [a.order_id for a in reconciled.activated] == [ORDER_ID]
    latest = approvals.latest_active(conversation.id)
    assert latest is not None  # approval activates on the reconciled delivery


def test_unknown_reconciled_by_failed_report(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    approvals.register_drafts(conversation.id, row.id, [ORDER_ID])
    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter(["unknown"])
    )
    processor.dispatch_conversation(conversation.id)
    reconciled = processor.apply_delivery_report(row.id, delivered=False)
    assert reconciled.row is not None and reconciled.row.status == "failed"
    assert reconciled.activated == []  # failure never activates approvals
    assert approvals.latest_active(conversation.id) is None


def test_duplicate_delivery_report_is_noop(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter(["sent"])
    )
    processor.dispatch_conversation(conversation.id)
    reconciled = processor.apply_delivery_report(row.id, delivered=True)
    assert reconciled.row is None and reconciled.activated == []


def test_dispatch_conversation_stays_within_conversation(parts) -> None:
    outbox, conversations, approvals = parts
    mine = _conversation(parts, PHONE)
    other = _conversation(parts, OTHER_PHONE)
    _enqueue(parts, other.id)
    my_row = _enqueue(parts, mine.id)
    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter(["sent"])
    )
    reports = processor.dispatch_conversation(mine.id)
    assert [r.outbox_id for r in reports] == [my_row.id]
    assert outbox.rows[my_row.id].status == "sent"


def test_dispatch_pending_sweeps_globally(parts) -> None:
    outbox, conversations, approvals = parts
    a = _conversation(parts, PHONE)
    b = _conversation(parts, OTHER_PHONE)
    _enqueue(parts, a.id)
    _enqueue(parts, b.id)
    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter(["sent", "sent"])
    )
    reports = processor.dispatch_pending(limit=10)
    assert len(reports) == 2


# -- Phase 3 remediation (M3): stale 'sending' recovery -------------------


def test_claim_stamps_the_lease_timestamp(parts) -> None:
    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter(["sent"])
    )
    processor.dispatch_conversation(conversation.id)
    assert outbox.rows[row.id].claimed_at is not None


def test_stale_sending_reconciles_to_unknown_never_resent(parts) -> None:
    """M3: a dispatcher that died mid-send leaves 'sending' past its
    lease. Recovery marks the row UNKNOWN (outcome cannot be determined)
    and never resends it automatically."""
    from datetime import UTC, datetime, timedelta

    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)

    # simulate a crashed dispatcher: claimed, never resolved
    with FakeTransaction() as conn:
        outbox.claim_next(conn, max_attempts=3)
    stale = outbox.rows[row.id].model_copy(
        update={"claimed_at": datetime.now(UTC) - timedelta(seconds=120)}
    )
    outbox.rows[row.id] = stale

    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter(["sent"])
    )
    reconciled = processor.reconcile_stale_sending()
    assert [r.id for r in reconciled] == [row.id]
    assert outbox.rows[row.id].status == "unknown"

    # a dispatch sweep does NOT pick the reconciled row back up
    reports = processor.dispatch_pending()
    assert reports == []
    # idempotent: nothing left in 'sending'
    assert processor.reconcile_stale_sending() == []


def test_fresh_sending_claim_is_left_alone(parts) -> None:
    """A live dispatcher's lease is respected: only expired claims
    reconcile."""
    from datetime import UTC, datetime, timedelta

    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    with FakeTransaction() as conn:
        outbox.claim_next(conn, max_attempts=3)
    fresh = outbox.rows[row.id].model_copy(
        update={"claimed_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    outbox.rows[row.id] = fresh

    processor = OutboxProcessor(
        outbox,
        conversations,
        approvals,
        adapter=FakeWhatsAppAdapter([]),
        stale_after_seconds=60.0,
    )
    assert processor.reconcile_stale_sending() == []
    assert outbox.rows[row.id].status == "sending"


def test_dispatch_reconciles_stale_before_sending(parts) -> None:
    """Every dispatch sweep reconciles expired claims first: crashed sends
    become 'unknown' instead of sitting invisible forever."""
    from datetime import UTC, datetime, timedelta

    outbox, conversations, approvals = parts
    conversation = _conversation(parts)
    row = _enqueue(parts, conversation.id)
    with FakeTransaction() as conn:
        outbox.claim_next(conn, max_attempts=3)
    stale = outbox.rows[row.id].model_copy(
        update={"claimed_at": datetime.now(UTC) - timedelta(seconds=120)}
    )
    outbox.rows[row.id] = stale

    processor = OutboxProcessor(
        outbox, conversations, approvals, adapter=FakeWhatsAppAdapter([])
    )
    reports = processor.dispatch_pending()
    assert [r.status for r in reports] == ["unknown"]
    assert outbox.rows[row.id].status == "unknown"
