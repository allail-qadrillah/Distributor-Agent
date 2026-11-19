from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

import psycopg

from app.schemas.order import (
    OrderCreate,
    OrderDetailResponse,
    OrderItemInsert,
    OrderItemResponse,
    OrderResponse,
)

# Exactly the OrderResponse columns — nothing more
_ORDER_COLUMNS = (
    "id, order_number, customer_id, status, total_amount, notes, "
    "confirmed_at, cancelled_at, delivered_at, created_at, updated_at"
)
# Exactly the OrderItemResponse columns, including generated subtotal
_ITEM_COLUMNS = "id, order_id, product_id, quantity, unit_price, subtotal, created_at"

_INSERT_ORDER = f"""
INSERT INTO orders (customer_id, notes)
VALUES (%s, %s)
RETURNING {_ORDER_COLUMNS}
"""
_INSERT_ITEM = f"""
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
VALUES (%s, %s, %s, %s)
RETURNING {_ITEM_COLUMNS}
"""
_UPDATE_TOTAL = f"""
UPDATE orders SET total_amount = %s WHERE id = %s
RETURNING {_ORDER_COLUMNS}
"""
_GET_ORDER = f"SELECT {_ORDER_COLUMNS} FROM orders WHERE id = %s"
_GET_ORDER_FOR_UPDATE = f"SELECT {_ORDER_COLUMNS} FROM orders WHERE id = %s FOR UPDATE"
# Exposure input for the confirm workflow's credit check (service
# composes balance + this sum + the order total).
_SUM_CONFIRMED_TOTALS = """
SELECT COALESCE(SUM(total_amount), 0.00) AS confirmed_total
FROM orders WHERE customer_id = %s AND status = 'confirmed'
"""
# Guarded transition (migration 001 pattern): zero rows affected means
# the transition already happened or is illegal.
_MARK_CONFIRMED = f"""
UPDATE orders SET status = 'confirmed', confirmed_at = now()
WHERE id = %s AND status = 'draft'
RETURNING {_ORDER_COLUMNS}
"""
# created_at is transaction-start time in Postgres: all items of one order
# share it, so id is the real tiebreaker. Stable, deterministic output.
_GET_ITEMS = f"SELECT {_ITEM_COLUMNS} FROM order_items WHERE order_id = %s ORDER BY created_at, id"


class OrderRepository:
    def create_order(
        self, conn: psycopg.Connection, order: OrderCreate
    ) -> OrderResponse:
        with conn.cursor() as cur:
            cur.execute(_INSERT_ORDER, (order.customer_id, order.notes))
            row = cur.fetchone()
        return OrderResponse.model_validate(row)

    def create_order_items(
        self, conn: psycopg.Connection, order_id: UUID, items: Sequence[OrderItemInsert]
    ) -> list[OrderItemResponse]:
        with conn.cursor() as cur:
            rows = []
            for item in items:
                cur.execute(
                    _INSERT_ITEM,
                    (order_id, item.product_id, item.quantity, item.unit_price),
                )
                rows.append(cur.fetchone())
        return [OrderItemResponse.model_validate(row) for row in rows]

    def update_total_amount(
        self, conn: psycopg.Connection, order_id: UUID, total_amount: Decimal
    ) -> OrderResponse | None:
        with conn.cursor() as cur:
            cur.execute(_UPDATE_TOTAL, (total_amount, order_id))
            row = cur.fetchone()
        return OrderResponse.model_validate(row) if row else None

    def get_for_update(
        self, conn: psycopg.Connection, order_id: UUID
    ) -> OrderDetailResponse | None:
        with conn.cursor() as cur:
            cur.execute(_GET_ORDER_FOR_UPDATE, (order_id,))
            order_row = cur.fetchone()
            if order_row is None:
                return None
            cur.execute(_GET_ITEMS, (order_id,))
            item_rows = cur.fetchall()
        return OrderDetailResponse.model_validate({**order_row, "items": item_rows})

    def sum_confirmed_totals(
        self, conn: psycopg.Connection, customer_id: UUID
    ) -> Decimal:
        with conn.cursor() as cur:
            cur.execute(_SUM_CONFIRMED_TOTALS, (customer_id,))
            row = cur.fetchone()
        return row["confirmed_total"]

    def mark_confirmed(
        self, conn: psycopg.Connection, order_id: UUID
    ) -> OrderResponse | None:
        with conn.cursor() as cur:
            cur.execute(_MARK_CONFIRMED, (order_id,))
            row = cur.fetchone()
        return OrderResponse.model_validate(row) if row else None

    def get_by_id(
        self, conn: psycopg.Connection, order_id: UUID
    ) -> OrderDetailResponse | None:
        with conn.cursor() as cur:
            cur.execute(_GET_ORDER, (order_id,))
            order_row = cur.fetchone()
            if order_row is None:
                return None
            cur.execute(_GET_ITEMS, (order_id,))
            item_rows = cur.fetchall()
        return OrderDetailResponse.model_validate({**order_row, "items": item_rows})
