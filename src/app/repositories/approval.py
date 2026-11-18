"""Order approval repository — SQL for the order_approvals table.

Approval lifecycle (policy lives in ApprovalManager):
pending_delivery -> active (only on confirmed delivery of the summary
message) -> consumed (single-use, guarded). UNIQUE (order_id) makes one
approval row per order, ever.
"""

from uuid import UUID

import psycopg

from app.schemas.conversation import ApprovalStatus, OrderApprovalResponse

_COLUMNS = (
    "id, conversation_id, order_id, delivery_message_id, status, "
    "activated_at, consumed_at, created_at, updated_at"
)

_INSERT = f"""
INSERT INTO order_approvals (conversation_id, order_id, delivery_message_id)
VALUES (%(conversation_id)s, %(order_id)s, %(delivery_message_id)s)
ON CONFLICT (order_id) DO NOTHING
RETURNING {_COLUMNS}
"""


class ApprovalRepository:
    def create_pending(
        self,
        conn: psycopg.Connection,
        conversation_id: UUID,
        order_id: UUID,
        delivery_message_id: UUID,
    ) -> OrderApprovalResponse | None:
        """Register approval intent for one order awaiting its summary
        delivery. None = an approval row for this order already exists
        (idempotent re-run, the first row keeps its own delivery link)."""
        with conn.cursor() as cur:
            cur.execute(
                _INSERT,
                {
                    "conversation_id": conversation_id,
                    "order_id": order_id,
                    "delivery_message_id": delivery_message_id,
                },
            )
            row = cur.fetchone()
        return OrderApprovalResponse.model_validate(row) if row else None

    def activate_for_delivery(
        self, conn: psycopg.Connection, delivery_message_id: UUID
    ) -> list[OrderApprovalResponse]:
        """Activate every pending approval whose summary message was
        confirmed delivered. Guarded from pending_delivery: activation
        happens exactly once, only from the delivered state."""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE order_approvals
                SET status = 'active', activated_at = now(), updated_at = now()
                WHERE delivery_message_id = %s AND status = 'pending_delivery'
                RETURNING {_COLUMNS}
                """,
                (delivery_message_id,),
            )
            rows = cur.fetchall()
        return [OrderApprovalResponse.model_validate(r) for r in rows]

    def latest_active_by_conversation(
        self, conn: psycopg.Connection, conversation_id: UUID
    ) -> OrderApprovalResponse | None:
        """The most recently activated approval for the conversation; the
        order the customer is most plausibly answering."""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_COLUMNS} FROM order_approvals
                WHERE conversation_id = %s AND status = 'active'
                ORDER BY activated_at DESC, id
                LIMIT 1
                """,
                (conversation_id,),
            )
            row = cur.fetchone()
        return OrderApprovalResponse.model_validate(row) if row else None

    def consume(
        self, conn: psycopg.Connection, conversation_id: UUID, order_id: UUID
    ) -> OrderApprovalResponse | None:
        """Single-use consumption: guarded from 'active'. None means the
        approval was not active (already consumed, still pending delivery,
        or never existed for this conversation+order)."""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE order_approvals
                SET status = 'consumed', consumed_at = now(), updated_at = now()
                WHERE conversation_id = %s AND order_id = %s AND status = 'active'
                RETURNING {_COLUMNS}
                """,
                (conversation_id, order_id),
            )
            row = cur.fetchone()
        return OrderApprovalResponse.model_validate(row) if row else None

    def list_by_conversation(
        self, conn: psycopg.Connection, conversation_id: UUID
    ) -> list[OrderApprovalResponse]:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_COLUMNS} FROM order_approvals
                WHERE conversation_id = %s
                ORDER BY created_at, id
                """,
                (conversation_id,),
            )
            rows = cur.fetchall()
        return [OrderApprovalResponse.model_validate(r) for r in rows]

    def get_status_for_order(
        self, conn: psycopg.Connection, order_id: UUID
    ) -> ApprovalStatus | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM order_approvals WHERE order_id = %s", (order_id,)
            )
            row = cur.fetchone()
        return row["status"] if row else None

    def has_pending_for_conversation(
        self, conn: psycopg.Connection, conversation_id: UUID
    ) -> bool:
        """True when any approval of this conversation still awaits its
        summary delivery: the deferral window (M1). A reply that arrives
        in this window is deferred, not lost."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM order_approvals "
                "WHERE conversation_id = %s AND status = 'pending_delivery' LIMIT 1",
                (conversation_id,),
            )
            return cur.fetchone() is not None
