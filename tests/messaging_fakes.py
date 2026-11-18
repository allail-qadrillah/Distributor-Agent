"""Shared fakes for the messaging-layer unit tests.

In-memory repositories that mirror the SQL semantics that matter for the
workflows: UNIQUE (phone), UNIQUE (external_message_id), UNIQUE
(message_id) on the outbox, guarded state transitions, claim budgets.
SQL-level guarantees (SKIP LOCKED, constraint enforcement) are proven by
the RUN_DB_SMOKE integration tests, not here.

The FakeTransaction stands in for app.database.connection.transaction()
exactly like tests/test_order_service.py does: yields one shared conn
token, records commit/rollback, re-raises exceptions.
"""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import app.messaging.approval as approval_module
import app.messaging.conversation_service as conversation_service_module
import app.messaging.outbox as outbox_module
from app.agents.schemas import AgentRunResult
from app.schemas.conversation import (
    ConversationMessageResponse,
    ConversationResponse,
    MessageInsert,
    OrderApprovalResponse,
    OutboxMessageResponse,
    OutboxStatus,
)
from app.schemas.customer import CustomerResponse
from app.tools.context import ToolContext
from app.whatsapp.schemas import WhatsAppInboundMessage


class FakeConn:
    """Connection identity token shared by one transaction."""


class FakeTransaction:
    def __init__(self) -> None:
        self.conn = FakeConn()
        self.journal: list[str] = []

    def __call__(self) -> "FakeTransaction":
        return self

    def __enter__(self) -> FakeConn:
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.journal.append("commit" if exc_type is None else "rollback")
        return False


def patch_transaction(monkeypatch, tx: FakeTransaction) -> None:
    """Point every messaging module's `transaction` at the fake."""
    monkeypatch.setattr(conversation_service_module, "transaction", tx)
    monkeypatch.setattr(approval_module, "transaction", tx)
    monkeypatch.setattr(outbox_module, "transaction", tx)


def null_lock(phone: str) -> object:
    return nullcontext()


class RecordingLock:
    """Serializes nothing; records lock acquisitions per phone."""

    def __init__(self) -> None:
        self.acquired: list[str] = []

    def __call__(self, phone: str) -> object:
        self.acquired.append(phone)
        return nullcontext()


class FakeConversationRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, ConversationResponse] = {}
        self._tick = datetime(2026, 11, 18, 8, 0, 0, tzinfo=UTC)

    def _now(self) -> datetime:
        self._tick += timedelta(seconds=1)
        return self._tick

    def get_or_create_by_phone(
        self, conn, phone: str, customer_id: UUID | None = None
    ) -> ConversationResponse:
        for row in self.rows.values():
            if row.phone == phone:
                return row
        row = ConversationResponse(
            id=uuid4(),
            phone=phone,
            customer_id=customer_id,
            state="active",
            handoff_reason=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        self.rows[row.id] = row
        return row

    def link_customer(self, conn, conversation_id, customer_id):
        row = self.rows.get(conversation_id)
        if row is None or row.customer_id is not None:
            return None
        row = row.model_copy(
            update={"customer_id": customer_id, "updated_at": self._now()}
        )
        self.rows[conversation_id] = row
        return row

    def get_by_id(self, conn, conversation_id):
        return self.rows.get(conversation_id)

    def list_conversations(self, conn, state=None, limit=50, offset=0):
        rows = [r for r in self.rows.values() if state is None or r.state == state]
        return rows[offset : offset + limit]

    def set_state(self, conn, conversation_id, from_states, to_state, reason=None):
        row = self.rows.get(conversation_id)
        if row is None or row.state not in from_states:
            return None
        row = row.model_copy(
            update={
                "state": to_state,
                "handoff_reason": reason,
                "updated_at": self._now(),
            }
        )
        self.rows[conversation_id] = row
        return row


class FakeMessageRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, ConversationMessageResponse] = {}
        self.external_ids: set[str] = set()
        self._seq = 0
        self._tick = datetime(2026, 11, 18, 8, 0, 0, tzinfo=UTC)

    def _next(self) -> tuple[int, datetime]:
        self._seq += 1
        self._tick += timedelta(seconds=1)
        return self._seq, self._tick

    def claim_inbound(self, conn, conversation_id, external_message_id, content):
        if external_message_id in self.external_ids:
            return None  # UNIQUE (external_message_id): the atomic claim
        seq, now = self._next()
        row = ConversationMessageResponse(
            id=uuid4(),
            conversation_id=conversation_id,
            seq=seq,
            direction="inbound",
            role="user",
            content=content,
            tool_call_id=None,
            tool_calls=None,
            external_message_id=external_message_id,
            status="unread",
            created_at=now,
        )
        self.external_ids.add(external_message_id)
        self.rows[row.id] = row
        return row

    def append_message(self, conn, conversation_id, message: MessageInsert):
        seq, now = self._next()
        row = ConversationMessageResponse(
            id=uuid4(),
            conversation_id=conversation_id,
            seq=seq,
            direction=message.direction,
            role=message.role,
            content=message.content,
            tool_call_id=message.tool_call_id,
            tool_calls=message.tool_calls,
            external_message_id=None,
            status=message.status,
            created_at=now,
        )
        self.rows[row.id] = row
        return row

    def list_by_conversation(
        self, conn, conversation_id, before_seq=None, limit=None, roles=None
    ):
        rows = [
            r
            for r in self.rows.values()
            if r.conversation_id == conversation_id
            and (before_seq is None or r.seq < before_seq)
            and (roles is None or (r.role in roles and r.content is not None))
        ]
        rows.sort(key=lambda r: r.seq)
        if limit is not None:
            rows = rows[-limit:]  # newest N, like the SQL DESC+LIMIT path
        return rows

    def list_unread_inbound(self, conn, conversation_id):
        rows = [
            r
            for r in self.rows.values()
            if r.conversation_id == conversation_id
            and r.direction == "inbound"
            and r.status == "unread"
        ]
        rows.sort(key=lambda r: r.seq)
        return rows

    def mark_read(self, conn, message_id):
        row = self.rows.get(message_id)
        if row is None or row.status != "unread":
            return None
        row = row.model_copy(update={"status": "read"})
        self.rows[message_id] = row
        return row


class FakeOutboxRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, OutboxMessageResponse] = {}
        self.message_ids: set[UUID] = set()
        self._tick = datetime(2026, 11, 18, 8, 0, 0, tzinfo=UTC)

    def _now(self) -> datetime:
        self._tick += timedelta(seconds=1)
        return self._tick

    def create(self, conn, message_id, conversation_id, body):
        if message_id in self.message_ids:
            raise psycopg_unique_violation()  # UNIQUE (message_id)
        self.message_ids.add(message_id)
        row = OutboxMessageResponse(
            id=uuid4(),
            message_id=message_id,
            conversation_id=conversation_id,
            body=body,
            status="pending",
            attempts=0,
            last_error=None,
            sent_at=None,
            claimed_at=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        self.rows[row.id] = row
        return row

    def claim_next(self, conn, max_attempts, conversation_id=None):
        candidates = sorted(self.rows.values(), key=lambda r: (r.created_at, r.id))
        for row in candidates:
            if conversation_id is not None and row.conversation_id != conversation_id:
                continue
            dispatchable = row.status == "pending" or (
                row.status == "failed" and row.attempts < max_attempts
            )
            if dispatchable:
                row = row.model_copy(
                    update={
                        "status": "sending",
                        "attempts": row.attempts + 1,
                        "claimed_at": self._now(),
                        "updated_at": self._now(),
                    }
                )
                self.rows[row.id] = row
                return row
        return None

    def reconcile_stale_sending(self, conn, stale_after_seconds):
        now = datetime.now(UTC)
        reconciled = []
        for row in list(self.rows.values()):
            if row.status != "sending" or row.claimed_at is None:
                continue
            age = (now - row.claimed_at).total_seconds()
            if age > stale_after_seconds:
                row = row.model_copy(
                    update={
                        "status": "unknown",
                        "last_error": "stale sending claim reconciled",
                        "updated_at": now,
                    }
                )
                self.rows[row.id] = row
                reconciled.append(row)
        return reconciled

    def record_send_result(self, conn, outbox_id, status, last_error=None):
        row = self.rows.get(outbox_id)
        if row is None or row.status != "sending":
            return None
        row = row.model_copy(
            update={
                "status": status,
                "last_error": last_error,
                "sent_at": self._now() if status == "sent" else row.sent_at,
                "updated_at": self._now(),
            }
        )
        self.rows[outbox_id] = row
        return row

    def apply_delivery_report(self, conn, outbox_id, delivered):
        row = self.rows.get(outbox_id)
        if row is None or row.status not in ("pending", "sending", "unknown"):
            return None
        status: OutboxStatus = "sent" if delivered else "failed"
        row = row.model_copy(
            update={
                "status": status,
                "sent_at": self._now() if delivered else row.sent_at,
                "updated_at": self._now(),
            }
        )
        self.rows[outbox_id] = row
        return row


class psycopg_unique_violation(Exception):
    """Stands in for the UNIQUE constraint violation the real table raises."""


class FakeApprovalRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, OrderApprovalResponse] = {}
        self.order_ids: set[UUID] = set()
        self._tick = datetime(2026, 11, 18, 8, 0, 0, tzinfo=UTC)

    def _now(self) -> datetime:
        self._tick += timedelta(seconds=1)
        return self._tick

    def create_pending(self, conn, conversation_id, order_id, delivery_message_id):
        if order_id in self.order_ids:
            return None  # UNIQUE (order_id): idempotent registration
        self.order_ids.add(order_id)
        row = OrderApprovalResponse(
            id=uuid4(),
            conversation_id=conversation_id,
            order_id=order_id,
            delivery_message_id=delivery_message_id,
            status="pending_delivery",
            activated_at=None,
            consumed_at=None,
            created_at=self._now(),
            updated_at=self._now(),
        )
        self.rows[row.id] = row
        return row

    def activate_for_delivery(self, conn, delivery_message_id):
        activated = []
        for row in self.rows.values():
            if (
                row.delivery_message_id == delivery_message_id
                and row.status == "pending_delivery"
            ):
                row = row.model_copy(
                    update={
                        "status": "active",
                        "activated_at": self._now(),
                        "updated_at": self._now(),
                    }
                )
                self.rows[row.id] = row
                activated.append(row)
        return activated

    def latest_active_by_conversation(self, conn, conversation_id):
        active = [
            r
            for r in self.rows.values()
            if r.conversation_id == conversation_id and r.status == "active"
        ]
        if not active:
            return None
        return max(active, key=lambda r: (r.activated_at, r.id))

    def consume(self, conn, conversation_id, order_id):
        for row in self.rows.values():
            if (
                row.conversation_id == conversation_id
                and row.order_id == order_id
                and row.status == "active"
            ):
                row = row.model_copy(
                    update={
                        "status": "consumed",
                        "consumed_at": self._now(),
                        "updated_at": self._now(),
                    }
                )
                self.rows[row.id] = row
                return row
        return None

    def has_pending_for_conversation(self, conn, conversation_id):
        return any(
            r.conversation_id == conversation_id and r.status == "pending_delivery"
            for r in self.rows.values()
        )


class FakeCustomerRepository:
    def __init__(self, customers: dict[str, CustomerResponse] | None = None) -> None:
        self.by_phone: dict[str, CustomerResponse] = customers or {}

    def get_by_phone(self, conn, phone):
        return self.by_phone.get(phone)


class FakeOrchestrator:
    """Scripted agent. One script entry per run: a list of NEW protocol
    messages (built into a full AgentRunResult over the input), or an
    exception to raise. An AgentError with a `new_messages` attribute
    gets those messages attached as partial_transcript, exactly like the
    real orchestrator does for a failed run."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[tuple[object, ToolContext]] = []

    def run(self, agent_input, context):

        self.calls.append((agent_input, context))
        entry = self.script.pop(0) if self.script else []
        if isinstance(entry, Exception):
            new_messages = getattr(entry, "new_messages", None)
            if new_messages:
                entry.partial_transcript = [*agent_input.messages, *new_messages]
                entry.partial_events = []
            raise entry
        new = list(entry)
        final_text = ""
        for message in reversed(new):
            if message.role == "assistant" and message.content:
                final_text = message.content
                break
        return AgentRunResult(
            final_text=final_text,
            transcript=[*agent_input.messages, *new],
        )


class StubClassifier:
    """Returns one scripted IntentClassification (or raises), records
    what the gate asked it to classify."""

    def __init__(self, classification=None, error=None) -> None:
        from app.intent.schemas import IntentClassification

        self.classification = (
            classification
            if classification is not None
            else IntentClassification(
                category="order_related", confidence=0.99, reason="test"
            )
        )
        self.error = error
        self.asked: list[tuple[str, list]] = []

    def classify(self, message, context=()):
        self.asked.append((message, list(context)))
        if self.error is not None:
            raise self.error
        return self.classification


class FakeWhatsAppAdapter:
    """Scripted outbound transport; records (outbox_id, to, body). The
    outcome list must cover every attempt: an unscripted send fails the
    test instead of silently succeeding."""

    def __init__(
        self, outcomes: list[Literal["sent", "failed", "unknown"]] | None = None
    ) -> None:
        self.outcomes = list(outcomes or [])
        self.sent: list[tuple[UUID, str, str]] = []

    def send(self, outbox_id, to, body):
        from app.whatsapp.adapter import WhatsAppSendOutcome

        self.sent.append((outbox_id, to, body))
        assert self.outcomes, "no scripted send outcome for this attempt"
        status = self.outcomes.pop(0)
        return WhatsAppSendOutcome(
            status=status, error=None if status == "sent" else "bridge down"
        )


def make_customer(
    phone: str, customer_id: UUID | None = None, active: bool = True
) -> CustomerResponse:
    return CustomerResponse(
        id=customer_id or uuid4(),
        code="CUST-001",
        name="Toko Sinar Jaya",
        phone=phone,
        address=None,
        credit_limit=None,
        outstanding_balance=Decimal("0.00"),
        price_list_id=None,
        is_active=active,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        updated_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


def inbound(phone: str, external_id: str, text: str) -> WhatsAppInboundMessage:
    return WhatsAppInboundMessage(
        external_message_id=external_id, phone=phone, text=text
    )
