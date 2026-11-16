"""Order repository — database access for orders and order_items.

Rules (same conventions as other repositories):
- Database operations only. No business rules: no price resolution, no
  inventory reservation, no credit logic, no status transitions, no LLM,
  no API.
- Parameterized SQL only (never string-formatted).
- Returns Pydantic schemas, None for missing single records, lists for
  children; never raw cursors.
- Psycopg exceptions propagate (never swallowed) — user-safe mapping is
  the API layer's job.

Connection injection (deliberate difference from the other repositories):
- Every method receives the caller's connection and uses it for all
  operations. This module never opens a connection and never manages
  transaction lifecycle: no BEGIN, no COMMIT, no ROLLBACK. The caller
  (future OrderService) owns the transaction so multi-table writes
  (orders + order_items + total_amount) stay atomic.
- The connection must use dict_row (app.database.connection.connect and
  transaction() provide it).

Generated values stay generated:
- subtotal is a database generated column: read back via RETURNING,
  never computed in Python.
- order_number, status, total_amount, timestamps come from database
  defaults; the repository never writes them.
"""

from collections.abc import Sequence
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
_GET_ORDER = f"SELECT {_ORDER_COLUMNS} FROM orders WHERE id = %s"
# created_at is transaction-start time in Postgres: all items of one order
# share it, so id is the real tiebreaker. Stable, deterministic output.
_GET_ITEMS = f"SELECT {_ITEM_COLUMNS} FROM order_items WHERE order_id = %s ORDER BY created_at, id"


class OrderRepository:
    def create_order(
        self, conn: psycopg.Connection, order: OrderCreate
    ) -> OrderResponse:
        """Insert one draft order row; return it as stored.

        order.items are NOT written here — create_order_items() inserts
        them; OrderCreate is accepted whole because it is the draft-order
        input. status, order_number, total_amount and timestamps come
        from database defaults. No commit/rollback: the caller owns the
        transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_INSERT_ORDER, (order.customer_id, order.notes))
            row = cur.fetchone()
        return OrderResponse.model_validate(row)

    def create_order_items(
        self, conn: psycopg.Connection, order_id: UUID, items: Sequence[OrderItemInsert]
    ) -> list[OrderItemResponse]:
        """Insert all order items; return them as stored by the database.

        unit_price is the service-resolved snapshot; subtotal comes back
        from the generated column via RETURNING — never computed here.
        One INSERT..RETURNING per item keeps the 1:1 input->row mapping:
        psycopg 3.3's executemany(returning=True) only exposes the first
        statement's rows. ponytail: swap for a single INSERT..SELECT
        unnest(...) RETURNING if item counts ever make the round trips
        matter. No commit/rollback: the caller owns the transaction.
        """
        with conn.cursor() as cur:
            rows = []
            for item in items:
                cur.execute(
                    _INSERT_ITEM,
                    (order_id, item.product_id, item.quantity, item.unit_price),
                )
                rows.append(cur.fetchone())
        return [OrderItemResponse.model_validate(row) for row in rows]

    def get_by_id(
        self, conn: psycopg.Connection, order_id: UUID
    ) -> OrderDetailResponse | None:
        """Return the order with this id plus its items, or None.

        Two statements on the supplied connection: order and items read
        inside the caller's transaction, so the result is consistent.
        """
        with conn.cursor() as cur:
            cur.execute(_GET_ORDER, (order_id,))
            order_row = cur.fetchone()
            if order_row is None:
                return None
            cur.execute(_GET_ITEMS, (order_id,))
            item_rows = cur.fetchall()
        return OrderDetailResponse.model_validate({**order_row, "items": item_rows})
