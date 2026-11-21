"""Phase 3 conversation layer live smoke tests — NOT run by default.

Manual run (self-cleaning, touches only its own throwaway rows):

    RUN_DB_SMOKE=1 uv run pytest tests/integration/test_conversation_integration.py -v

Requirements:
- Migrations 001 + 002 + 003 + 004 applied to Supabase
- .env SUPABASE_DB_URL configured and reachable

Safety:
- Every test uses a random E.164 phone and random UUIDs; teardown deletes
  its own conversations row (cascades messages/outbox/approvals), plus
  its own throwaway customer and order rows for the approval test.
- No agent, no classifier, no WhatsApp bridge is contacted: the adapter
  is the scripted FakeWhatsAppAdapter from tests/messaging_fakes.py.

What is proven here (SQL-level, beyond the unit fakes):
- UNIQUE (phone) conversation identity and the lazy customer link;
- atomic inbound claim: ON CONFLICT (external_message_id) DO NOTHING;
- transcript persistence and reconstruction over real seq identity,
  including tool_calls jsonb round-trip;
- the handoff state machine over guarded UPDATEs;
- outbox claim semantics: failed re-claimed within budget, unknown never
  claimed, delivery report reconciliation;
- the approval lifecycle bound to one order and one delivery message;
- per-phone advisory locking with pg_try_advisory_lock.
"""

import os
from uuid import UUID, uuid4

import pytest

from app.agents.schemas import AgentMessage, AgentToolCall
from app.database.connection import connect, transaction
from app.messaging.approval import ApprovalManager
from app.messaging.conversation_service import ConversationService
from app.messaging.exceptions import InvalidConversationStateError
from app.messaging.handoff import HandoffManager
from app.messaging.identity import IdentityResolver
from app.messaging.locking import _lock_key, conversation_lock
from app.messaging.outbox import OutboxProcessor
from app.repositories.approval import ApprovalRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.customer import CustomerRepository
from app.repositories.message import MessageRepository
from app.repositories.outbox import OutboxRepository
from tests.messaging_fakes import FakeWhatsAppAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_SMOKE") != "1",
    reason="live Supabase smoke test: set RUN_DB_SMOKE=1 to run",
)


def _phone() -> str:
    return f"+62897{uuid4().int % 1_000_000_000:09d}"


def _other_phone() -> str:
    return f"+62898{uuid4().int % 1_000_000_000:09d}"


def _conversations() -> ConversationService:
    return ConversationService(
        ConversationRepository(),
        MessageRepository(),
        OutboxRepository(),
        IdentityResolver(CustomerRepository()),
    )


def _cleanup(conn, phone: str, customer_id: UUID | None = None) -> None:
    # conversations row cascades messages, outbox, and order_approvals
    conn.execute("DELETE FROM conversations WHERE phone = %s", (phone,))
    if customer_id is not None:
        conn.execute("DELETE FROM orders WHERE customer_id = %s", (customer_id,))
        conn.execute("DELETE FROM customers WHERE id = %s", (customer_id,))


def _insert_customer(conn, cust_id: UUID, phone: str) -> None:
    conn.execute(
        "INSERT INTO customers (id, code, name, phone) VALUES (%s, %s, %s, %s)",
        (cust_id, f"SMOKEP3-{cust_id.hex[:8]}", "Smoke P3 Customer", phone),
    )


def _insert_draft_order(conn, order_id: UUID, cust_id: UUID) -> None:
    conn.execute(
        "INSERT INTO orders (id, customer_id, status, notes) "
        "VALUES (%s, %s, 'draft', 'phase3 approval smoke')",
        (order_id, cust_id),
    )


def _agent_run(final_text: str = "Ringkasan pesanan.") -> list[AgentMessage]:
    return [
        AgentMessage(
            role="assistant",
            tool_calls=[
                AgentToolCall(id="call-1", name="create_draft_order", arguments="{}")
            ],
        ),
        AgentMessage(role="tool", tool_call_id="call-1", content='{"ok": true}'),
        AgentMessage(role="assistant", content=final_text),
    ]


def test_conversation_identity_and_lazy_link():
    phone = _phone()
    cust_id = uuid4()
    try:
        with transaction() as conn:
            _insert_customer(conn, cust_id, phone)
        with transaction() as conn:
            repo = ConversationRepository()
            first = repo.get_or_create_by_phone(conn, phone)
            second = repo.get_or_create_by_phone(conn, phone)
            assert first.id == second.id  # UNIQUE (phone): one permanent row
        assert first.customer_id is None  # unknown sender: no fake customer

        with transaction() as conn:
            repo = ConversationRepository()
            linked = repo.link_customer(conn, first.id, cust_id)
            assert linked is not None and linked.customer_id == cust_id
            # guarded: an existing link is never overwritten
            assert repo.link_customer(conn, first.id, uuid4()) is None
    finally:
        with transaction() as conn:
            _cleanup(conn, phone, cust_id)


def test_inbound_claim_is_atomic_and_idempotent():
    phone = _phone()
    other_phone = _other_phone()
    conversations = _conversations()
    try:
        first = conversations.ingest_inbound(phone, "P3-A", "pesan pertama")
        assert first.message is not None and first.message.status == "unread"
        assert first.customer is None  # unregistered sender

        duplicate = conversations.ingest_inbound(phone, "P3-A", "pesan pertama")
        assert duplicate.message is None  # ON CONFLICT DO NOTHING: the claim lost
        assert duplicate.conversation.id == first.conversation.id
        assert duplicate.duplicate is not None
        assert duplicate.duplicate.id == first.message.id

        other = conversations.ingest_inbound(other_phone, "P3-A", "pesan salin")
        assert other.message is None
        assert other.duplicate is None

        second = conversations.ingest_inbound(phone, "P3-B", "pesan kedua")
        assert second.message is not None

        messages = conversations.list_messages(first.conversation.id)
        assert [m.content for m in messages] == ["pesan pertama", "pesan kedua"]
        assert [m.seq for m in messages] == sorted(m.seq for m in messages)
    finally:
        with transaction() as conn:
            _cleanup(conn, phone)
            _cleanup(conn, other_phone)


def test_transcript_roundtrip_with_tool_calls():
    phone = _phone()
    conversations = _conversations()
    try:
        inbound = conversations.ingest_inbound(phone, "P3-A", "pesan 10 dus")
        conversation_id = inbound.conversation.id
        run = conversations.persist_agent_run(conversation_id, _agent_run())
        assert run.outbound is not None
        assert run.outbound.body == "Ringkasan pesanan."
        assert run.outbound.status == "pending"

        transcript = conversations.load_transcript(conversation_id)
        assert [m.role for m in transcript] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert transcript[1].tool_calls is not None
        assert transcript[1].tool_calls[0].name == "create_draft_order"
        assert transcript[2].tool_call_id == "call-1"
        assert transcript[3].content == "Ringkasan pesanan."
    finally:
        with transaction() as conn:
            _cleanup(conn, phone)


def test_handoff_state_machine():
    phone = _phone()
    conversations = _conversations()
    handoff = HandoffManager(conversations)
    try:
        conversation_id = conversations.ingest_inbound(
            phone, "P3-A", "halo"
        ).conversation.id
        assert (
            handoff.request_handoff(conversation_id, reason="manual").state
            == "needs_human"
        )
        assert handoff.takeover(conversation_id).state == "human_active"
        assert handoff.resume(conversation_id).state == "active"
        with pytest.raises(InvalidConversationStateError):
            handoff.resume(conversation_id)  # active: no resume path
    finally:
        with transaction() as conn:
            _cleanup(conn, phone)


def test_outbox_recovery_semantics():
    phone = _phone()
    conversations = _conversations()
    approvals = ApprovalManager(ApprovalRepository())
    adapter = FakeWhatsAppAdapter(["failed", "failed", "failed"])
    outbox = OutboxProcessor(
        OutboxRepository(), ConversationRepository(), approvals, adapter
    )
    try:
        conversation_id = conversations.ingest_inbound(
            phone, "P3-A", "pesan"
        ).conversation.id
        run = conversations.persist_agent_run(conversation_id, _agent_run())
        outbox_id = run.outbound.id

        reports = outbox.dispatch_conversation(conversation_id)
        assert [r.status for r in reports] == ["failed"] * 3  # inline retry budget
        with transaction() as conn:
            row = OutboxRepository().get_by_id(conn, outbox_id)
        assert row.status == "failed" and row.attempts == 3

        # failed beyond budget: not claimable anymore
        assert outbox.dispatch_conversation(conversation_id) == []

        # 'failed' is terminal: the adapter CONFIRMED it never went out,
        # a late report cannot resurrect it (only 'unknown' reconciles)
        reconciliation = outbox.apply_delivery_report(outbox_id, delivered=True)
        assert reconciliation.row is None
        with transaction() as conn:
            row = OutboxRepository().get_by_id(conn, outbox_id)
        assert row.status == "failed"
    finally:
        with transaction() as conn:
            _cleanup(conn, phone)


def test_unknown_outbox_state_never_auto_resent():
    phone = _phone()
    conversations = _conversations()
    approvals = ApprovalManager(ApprovalRepository())
    adapter = FakeWhatsAppAdapter(["unknown"])
    outbox = OutboxProcessor(
        OutboxRepository(), ConversationRepository(), approvals, adapter
    )
    try:
        conversation_id = conversations.ingest_inbound(
            phone, "P3-A", "pesan"
        ).conversation.id
        run = conversations.persist_agent_run(conversation_id, _agent_run())
        outbox_id = run.outbound.id

        reports = outbox.dispatch_conversation(conversation_id)
        assert [r.status for r in reports] == ["unknown"]
        with transaction() as conn:
            row = OutboxRepository().get_by_id(conn, outbox_id)
        assert row.status == "unknown"
        # never re-claimed, never re-sent: reconciliation only
        assert outbox.dispatch_conversation(conversation_id) == []
        assert adapter.sent == [(outbox_id, phone, "Ringkasan pesanan.")]
        reconciled = outbox.apply_delivery_report(outbox_id, delivered=False)
        assert reconciled.row is not None and reconciled.row.status == "failed"
    finally:
        with transaction() as conn:
            _cleanup(conn, phone)


def test_approval_lifecycle_bound_to_delivery_and_order():
    phone = _phone()
    cust_id = uuid4()
    order_id = uuid4()
    conversations = _conversations()
    approvals = ApprovalManager(ApprovalRepository())
    adapter = FakeWhatsAppAdapter(["unknown"])
    outbox = OutboxProcessor(
        OutboxRepository(), ConversationRepository(), approvals, adapter
    )
    try:
        with transaction() as conn:
            _insert_customer(conn, cust_id, phone)
            _insert_draft_order(conn, order_id, cust_id)
        conversation_id = conversations.ingest_inbound(
            phone, "P3-A", "pesan 10 dus"
        ).conversation.id
        run = conversations.persist_agent_run(conversation_id, _agent_run())
        outbox_id = run.outbound.id

        approvals.register_drafts(conversation_id, outbox_id, [order_id])
        assert approvals.latest_active(conversation_id) is None

        outbox.dispatch_conversation(conversation_id)  # outcome unknown
        assert approvals.latest_active(conversation_id) is None  # pending_delivery

        # the bridge later confirms delivery: unknown reconciles, approval
        # activates IN THE SAME transaction (H3: they cannot diverge)
        reconciled = outbox.apply_delivery_report(outbox_id, delivered=True)
        assert reconciled.row is not None and reconciled.row.status == "sent"
        assert [a.order_id for a in reconciled.activated] == [order_id]
        active = approvals.latest_active(conversation_id)
        assert active is not None and active.order_id == order_id

        approvals.consume_confirmed(conversation_id, [order_id])
        assert approvals.latest_active(conversation_id) is None
        # single-use: a second consume finds nothing
        assert approvals.consume_confirmed(conversation_id, [order_id]) == []
    finally:
        with transaction() as conn:
            _cleanup(conn, phone, cust_id)


def test_per_phone_advisory_lock_serializes():
    phone = _phone()
    conversations = _conversations()
    try:
        conversations.ingest_inbound(phone, "P3-A", "pesan")  # phone row exists
        key = _lock_key(phone)
        with conversation_lock(phone):
            other = connect()
            try:
                other.autocommit = True
                with other.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,))
                    assert cur.fetchone()["ok"] is False  # held by our session
                    other_key = _lock_key(_phone())
                    cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (other_key,))
                    assert cur.fetchone()["ok"] is True  # other phones unaffected
                    cur.execute("SELECT pg_advisory_unlock(%s)", (other_key,))
            finally:
                other.close()
        with connect() as released:
            released.autocommit = True
            with released.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,))
                assert cur.fetchone()["ok"] is True  # released after the block
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    finally:
        with transaction() as conn:
            _cleanup(conn, phone)


def test_intent_context_slice_uses_seq():
    phone = _phone()
    conversations = _conversations()
    try:
        first = conversations.ingest_inbound(phone, "P3-A", "satu")
        conversations.ingest_inbound(phone, "P3-B", "dua")
        third = conversations.ingest_inbound(phone, "P3-C", "tiga")
        context = conversations.intent_context(
            first.conversation.id, before_seq=third.message.seq, limit=10
        )
        assert context == [("user", "satu"), ("user", "dua")]  # prior turns only
        limited = conversations.intent_context(
            first.conversation.id, before_seq=third.message.seq, limit=1
        )
        assert limited == [("user", "dua")]
    finally:
        with transaction() as conn:
            _cleanup(conn, phone)


# -- Phase 3 remediation: H3, H4, M3, M5 against the live database --------


def test_identity_mismatch_flags_without_relinking():
    """H4: a phone reassigned to another registered customer never
    replays the linked customer's transcript; the link survives."""
    phone = _phone()
    cust_id = uuid4()
    other_id = uuid4()
    conversations = _conversations()
    try:
        with transaction() as conn:
            _insert_customer(conn, cust_id, phone)
        first = conversations.ingest_inbound(phone, "P3-A", "pesan")
        assert first.customer is not None and first.identity_mismatch is False

        # the phone now resolves to a DIFFERENT registered customer
        with transaction() as conn:
            conn.execute(
                "UPDATE customers SET phone = %s WHERE id = %s",
                (_phone(), cust_id),
            )
            _insert_customer(conn, other_id, phone)
        second = conversations.ingest_inbound(phone, "P3-B", "pesan kedua")
        assert second.identity_mismatch is True
        assert second.customer is not None and second.customer.id == other_id
        # the permanent link is untouched
        assert second.conversation.customer_id == cust_id
        # the message is still persisted, unread
        assert second.message is not None and second.message.status == "unread"
    finally:
        with transaction() as conn:
            _cleanup(conn, phone, cust_id)
            _cleanup(conn, _phone(), other_id)


def test_persist_agent_run_registers_approvals_atomically():
    """H3: transcript + outbox + pending_delivery approvals in ONE
    transaction (draft_order_ids at persist time)."""
    phone = _phone()
    cust_id = uuid4()
    order_id = uuid4()
    conversations = ConversationService(
        ConversationRepository(),
        MessageRepository(),
        OutboxRepository(),
        IdentityResolver(CustomerRepository()),
        ApprovalRepository(),
    )
    try:
        with transaction() as conn:
            _insert_customer(conn, cust_id, phone)
            _insert_draft_order(conn, order_id, cust_id)
        conversation_id = conversations.ingest_inbound(
            phone, "P3-A", "pesan 10 dus"
        ).conversation.id
        run = conversations.persist_agent_run(
            conversation_id, _agent_run(), draft_order_ids=[order_id]
        )
        assert run.outbound is not None
        with transaction() as conn:
            status = ApprovalRepository().get_status_for_order(conn, order_id)
        assert status == "pending_delivery"
        approvals = ApprovalManager(ApprovalRepository())
        assert approvals.pending_exists(conversation_id) is True
    finally:
        with transaction() as conn:
            _cleanup(conn, phone, cust_id)


def test_stale_sending_reconciles_to_unknown():
    """M3: an expired claim lease reconciles to 'unknown' via real SQL
    (make_interval), and the row is never re-claimed."""
    phone = _phone()
    conversations = _conversations()
    outbox = OutboxProcessor(
        OutboxRepository(),
        ConversationRepository(),
        ApprovalManager(ApprovalRepository()),
        FakeWhatsAppAdapter([]),
        stale_after_seconds=60.0,
    )
    try:
        conversation_id = conversations.ingest_inbound(
            phone, "P3-A", "pesan"
        ).conversation.id
        run = conversations.persist_agent_run(conversation_id, _agent_run())
        outbox_id = run.outbound.id

        # a dispatcher claimed the row and died mid-send
        with transaction() as conn:
            OutboxRepository().claim_next(conn, max_attempts=3)
        with transaction() as conn:
            conn.execute(
                "UPDATE outbox_messages SET claimed_at = now() - "
                "make_interval(secs => 120) WHERE id = %s",
                (outbox_id,),
            )

        reconciled = outbox.reconcile_stale_sending()
        assert [r.id for r in reconciled] == [outbox_id]
        assert reconciled[0].status == "unknown"
        # never resent: a dispatch sweep finds nothing to claim
        assert outbox.dispatch_pending() == []
        # idempotent
        assert outbox.reconcile_stale_sending() == []
    finally:
        with transaction() as conn:
            _cleanup(conn, phone)


def test_load_agent_messages_bounds_window():
    """M5: bounded replay input over real seq ordering; the persisted
    transcript keeps everything."""
    phone = _phone()
    conversations = _conversations()
    try:
        conversation_id = conversations.ingest_inbound(
            phone, "P3-A", "satu"
        ).conversation.id
        conversations.persist_agent_run(conversation_id, _agent_run())
        conversations.ingest_inbound(phone, "P3-B", "dua")
        conversations.ingest_inbound(phone, "P3-C", "tiga")

        full = conversations.load_transcript(conversation_id)
        assert [m.role for m in full] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "user",
            "user",
        ]
        bounded = conversations.load_agent_messages(conversation_id, 3)
        assert [m.role for m in bounded] == ["assistant", "user", "user"]
    finally:
        with transaction() as conn:
            _cleanup(conn, phone)
