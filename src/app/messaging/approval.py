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
        if conn is not None:
            return self._approvals.activate_for_delivery(conn, outbox_id)
        with transaction() as own_conn:
            return self._approvals.activate_for_delivery(own_conn, outbox_id)

    def pending_exists(self, conversation_id: UUID) -> bool:
        with transaction() as conn:
            return self._approvals.has_pending_for_conversation(conn, conversation_id)

    def latest_active(self, conversation_id: UUID) -> OrderApprovalResponse | None:
        with transaction() as conn:
            return self._approvals.latest_active_by_conversation(conn, conversation_id)

    def consume_confirmed(
        self, conversation_id: UUID, order_ids: list[UUID]
    ) -> list[OrderApprovalResponse]:
        consumed: list[OrderApprovalResponse] = []
        with transaction() as conn:
            for order_id in order_ids:
                row = self._approvals.consume(conn, conversation_id, order_id)
                if row is not None:
                    consumed.append(row)
        return consumed
