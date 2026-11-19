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
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM order_approvals "
                "WHERE conversation_id = %s AND status = 'pending_delivery' LIMIT 1",
                (conversation_id,),
            )
            return cur.fetchone() is not None
