"""OutboxProcessor: outbound delivery state machine.

States (outbox_messages.status):
- pending: intent persisted, not yet sent.
- sending: atomically claimed by a dispatcher.
- sent: the adapter CONFIRMED successful delivery.
- failed: the adapter reported failure; retryable within the attempt
  budget, terminal beyond it.
- unknown: the send outcome is unknown (connection lost after the
  request). NOT failed: never auto-resent, only reconciled by a delivery
  report.

Why unknown is never resent: WhatsApp has no idempotency key for the
recipient; a blind resend of an unknown message can duplicate it. The
safe policy is reconciliation: the bridge reports the final status for
the outbox id (apply_delivery_report), the operator inspects. Retrying a
FAILED message is safe (the adapter reported it never went out) and
bounded by max_attempts.

Business-operation safety: dispatch only sends message TEXT. A retry
never re-executes an agent run or a business workflow; those already
happened before the outbox row existed.

The send itself is never inside a database transaction: claim (tx) ->
adapter call (no tx) -> record outcome (tx). A crash between claim and
record leaves a 'sending' row; a delivery report or operator action
reconciles it.
"""

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from app.database.connection import transaction
from app.messaging.approval import ApprovalManager
from app.repositories.conversation import ConversationRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.conversation import OrderApprovalResponse, OutboxMessageResponse
from app.whatsapp.adapter import WhatsAppAdapter, WhatsAppSendOutcome

SendResultStatus = Literal["sent", "failed", "unknown"]

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_STALE_AFTER_SECONDS = 60.0


@dataclass(frozen=True)
class SendReport:
    """One dispatched outbox row. activated is the number of approvals
    that became active because of THIS report (used by the API layer to
    trigger deferred-message reprocessing, M1)."""

    outbox_id: UUID
    status: SendResultStatus
    conversation_id: UUID | None = None
    activated: int = 0


@dataclass(frozen=True)
class DeliveryReconciliation:
    """Result of a bridge delivery report. row is None when the report
    matched an already-terminal row (duplicate or late report: no-op).
    activated lists the approvals activated by this report (H3: the
    outbox transition and the activation happened in ONE transaction)."""

    row: OutboxMessageResponse | None
    activated: list[OrderApprovalResponse] = field(default_factory=list)

    @property
    def activated_any(self) -> bool:
        return bool(self.activated)


class OutboxProcessor:
    def __init__(
        self,
        outbox: OutboxRepository,
        conversations: ConversationRepository,
        approvals: ApprovalManager,
        adapter: WhatsAppAdapter | None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        self._outbox = outbox
        self._conversations = conversations
        self._approvals = approvals
        self._adapter = adapter  # None = bridge not configured: nothing is sent
        self._max_attempts = max_attempts
        self._stale_after_seconds = stale_after_seconds

    def dispatch_conversation(self, conversation_id: UUID) -> list[SendReport]:
        """Send every dispatchable outbound message of one conversation
        (called inline after an agent reply). Order: oldest first. Stale
        'sending' rows are reconciled first (their reports belong to
        other conversations and are NOT returned here)."""
        self._reconcile_stale_if_configured()
        if self._adapter is None:
            return []
        reports: list[SendReport] = []
        while True:
            report = self._send_one(conversation_id=conversation_id)
            if report is None:
                return reports
            reports.append(report)

    def dispatch_pending(self, limit: int = 10) -> list[SendReport]:
        """Recovery sweep over ALL conversations (scheduler or operator
        endpoint): reconciles stale 'sending' rows, then drains up to
        `limit` dispatchable rows, oldest first."""
        reports = self._reconcile_stale_if_configured()
        if self._adapter is None:
            return reports
        while len(reports) < limit:
            report = self._send_one()
            if report is None:
                return reports
            reports.append(report)
        return reports

    def apply_delivery_report(
        self, outbox_id: UUID, delivered: bool
    ) -> DeliveryReconciliation:
        """Reconcile an adapter delivery report (late or resolving an
        'unknown'). The outbox transition AND the approval activation run
        in ONE transaction (H3): a crash can never leave a delivered
        summary with a permanently pending approval — a re-sent duplicate
        report re-runs the same guarded updates idempotently. Delivered
        transitions activate approvals; a None row means the row was
        already terminal (duplicate report)."""
        with transaction() as conn:
            row = self._outbox.apply_delivery_report(conn, outbox_id, delivered)
            activated = (
                self._approvals.activate_for_delivery(outbox_id, conn=conn)
                if row is not None and delivered
                else []
            )
        return DeliveryReconciliation(row, activated)

    def reconcile_stale_sending(self) -> list[OutboxMessageResponse]:
        """Reconcile expired 'sending' claims to 'unknown' (M3). Never
        sends anything: 'unknown' is never auto-resent; a delivery report
        (or an operator) resolves the final state. Idempotent."""
        with transaction() as conn:
            return self._outbox.reconcile_stale_sending(conn, self._stale_after_seconds)

    def _reconcile_stale_if_configured(self) -> list[SendReport]:
        rows = self.reconcile_stale_sending()
        return [
            SendReport(
                outbox_id=row.id,
                status="unknown",
                conversation_id=row.conversation_id,
            )
            for row in rows
        ]

    def _send_one(self, conversation_id: UUID | None = None) -> SendReport | None:
        with transaction() as conn:
            claimed = self._outbox.claim_next(
                conn, self._max_attempts, conversation_id=conversation_id
            )
            if claimed is None:
                return None
            conversation = self._conversations.get_by_id(conn, claimed.conversation_id)
        assert conversation is not None  # FK guarantees the row exists
        assert self._adapter is not None
        outcome: WhatsAppSendOutcome = self._adapter.send(
            outbox_id=claimed.id, to=conversation.phone, body=claimed.body
        )
        status = outcome.status
        # record the outcome AND activate approvals in ONE transaction:
        # 'sent' + approval-stuck-pending cannot diverge on a crash (H3)
        activated: list[OrderApprovalResponse] = []
        with transaction() as conn:
            self._outbox.record_send_result(conn, claimed.id, status, outcome.error)
            if status == "sent":
                activated = self._approvals.activate_for_delivery(claimed.id, conn=conn)
        return SendReport(
            outbox_id=claimed.id,
            status=status,
            conversation_id=claimed.conversation_id,
            activated=len(activated),
        )
