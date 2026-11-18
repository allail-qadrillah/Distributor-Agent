"""ApprovalManager: order approval lifecycle, one exact order at a time.

Lifecycle:
1. register_drafts: after a run created draft order(s) and its reply is
   the order summary, approval rows are created in pending_delivery,
   bound to (conversation, exact order, the outbox row that carries the
   summary). Creating a draft or an outbound intent is NOT approval.
2. activate_for_delivery: ONLY when the adapter confirms delivery of the
   summary message (outbox status -> sent). A failed or unknown delivery
   never activates anything.
3. consume_confirmed: single-use guarded consumption after the agent run
   actually confirmed the approved order. The ToolContext guard
   (approved_order_id == args.order_id) already refused any other order;
   consumption burns the approval so a later run cannot confirm again.

Known residual (accepted, documented): consumption runs in its own
transaction AFTER the confirmation transaction. A crash between them
leaves a stale active approval whose order is already confirmed; any
re-confirm attempt is refused by the order status guard
(InvalidOrderStatusError), so the residual is safe.
"""

from uuid import UUID

from app.database.connection import transaction
from app.repositories.approval import ApprovalRepository
from app.schemas.conversation import OrderApprovalResponse


class ApprovalManager:
    def __init__(self, approvals: ApprovalRepository) -> None:
        self._approvals = approvals

    def register_drafts(
        self,
        conversation_id: UUID,
        delivery_outbox_id: UUID,
        order_ids: list[UUID],
    ) -> list[OrderApprovalResponse]:
        """Create pending_delivery approvals for the run's new drafts.
        Idempotent per order (UNIQUE order_id): an existing approval row
        for the order is left untouched."""
        created: list[OrderApprovalResponse] = []
        with transaction() as conn:
            for order_id in order_ids:
                row = self._approvals.create_pending(
                    conn, conversation_id, order_id, delivery_outbox_id
                )
                if row is not None:
                    created.append(row)
        return created

    def activate_for_delivery(
        self, outbox_id: UUID, conn=None
    ) -> list[OrderApprovalResponse]:
        """Activate the approvals whose summary was confirmed delivered.
        Called with an open `conn` when the activation must be atomic with
        the outbox status transition (one transaction, H3); without a
        conn this method owns its own transaction. Guarded from
        pending_delivery, so a duplicate delivery event activates
        nothing the second time."""
        if conn is not None:
            return self._approvals.activate_for_delivery(conn, outbox_id)
        with transaction() as own_conn:
            return self._approvals.activate_for_delivery(own_conn, outbox_id)

    def pending_exists(self, conversation_id: UUID) -> bool:
        """True when an approval of this conversation still awaits its
        summary delivery: the deferral window (M1). Inbound replies in
        this window are deferred, not processed without approval."""
        with transaction() as conn:
            return self._approvals.has_pending_for_conversation(conn, conversation_id)

    def latest_active(self, conversation_id: UUID) -> OrderApprovalResponse | None:
        """The most recently activated approval: the order the customer is
        most plausibly answering. Injected into ToolContext as the ONE
        approved_order_id; every other order is refused by the handler."""
        with transaction() as conn:
            return self._approvals.latest_active_by_conversation(conn, conversation_id)

    def consume_confirmed(
        self, conversation_id: UUID, order_ids: list[UUID]
    ) -> list[OrderApprovalResponse]:
        """Burn approvals for orders the run actually confirmed.
        Guarded from active (single-use); a pending or already-consumed
        approval is skipped."""
        consumed: list[OrderApprovalResponse] = []
        with transaction() as conn:
            for order_id in order_ids:
                row = self._approvals.consume(conn, conversation_id, order_id)
                if row is not None:
                    consumed.append(row)
        return consumed
