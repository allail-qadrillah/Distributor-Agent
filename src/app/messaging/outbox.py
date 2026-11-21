import logging
import time
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from app.database.connection import transaction
from app.messaging.approval import ApprovalManager
from app.observability import log_event
from app.repositories.conversation import ConversationRepository
from app.repositories.outbox import OutboxRepository
from app.schemas.conversation import OrderApprovalResponse, OutboxMessageResponse
from app.whatsapp.adapter import WhatsAppAdapter, WhatsAppSendOutcome

_log = logging.getLogger(__name__)

SendResultStatus = Literal["sent", "failed", "unknown"]

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_STALE_AFTER_SECONDS = 60.0


@dataclass(frozen=True)
class SendReport:
    outbox_id: UUID
    status: SendResultStatus
    conversation_id: UUID | None = None
    activated: int = 0


@dataclass(frozen=True)
class DeliveryReconciliation:
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
        with transaction() as conn:
            row = self._outbox.apply_delivery_report(conn, outbox_id, delivered)
            activated = (
                self._approvals.activate_for_delivery(outbox_id, conn=conn)
                if row is not None and delivered
                else []
            )
        log_event(
            _log,
            "delivery_report_reconciled",
            outbox_id=str(outbox_id),
            delivered=delivered,
            status=row.status if row is not None else "already_final",
            activated=len(activated),
        )
        return DeliveryReconciliation(row, activated)

    def reconcile_stale_sending(self) -> list[OutboxMessageResponse]:
        with transaction() as conn:
            rows = self._outbox.reconcile_stale_sending(conn, self._stale_after_seconds)
        for row in rows:
            log_event(
                _log,
                "stale_sending_reconciled",
                level=logging.WARNING,
                outbox_id=str(row.id),
                conversation_id=(
                    str(row.conversation_id)
                    if row.conversation_id is not None
                    else None
                ),
            )
        return rows

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
            log_event(
                _log,
                "outbox_message_claimed",
                outbox_id=str(claimed.id),
                attempts=claimed.attempts,
            )
            conversation = self._conversations.get_by_id(conn, claimed.conversation_id)
        assert conversation is not None  # FK guarantees the row exists
        assert self._adapter is not None
        log_event(_log, "dispatch_started", outbox_id=str(claimed.id))
        started = time.monotonic()
        outcome: WhatsAppSendOutcome = self._adapter.send(
            outbox_id=claimed.id, to=conversation.phone, body=claimed.body
        )
        status = outcome.status
        duration_ms = int((time.monotonic() - started) * 1000)
        log_event(
            _log,
            {
                "sent": "dispatch_completed",
                "failed": "dispatch_failed",
                "unknown": "dispatch_outcome_unknown",
            }[status],
            level=(logging.INFO if status == "sent" else logging.ERROR),
            outbox_id=str(claimed.id),
            status=status,
            error=outcome.error,
            duration_ms=duration_ms,
        )
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
