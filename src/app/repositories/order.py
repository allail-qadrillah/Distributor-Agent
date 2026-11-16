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

    def update_total_amount(
        self, conn: psycopg.Connection, order_id: UUID, total_amount: Decimal
    ) -> OrderResponse | None:
        """Set the order's total; return the updated order, or None.

        total_amount is the Decimal sum of the database-generated item
        subtotals, bound as a parameter (numeric(14,2) precision). The
        trg_orders_upd trigger refreshes updated_at. None means no order
        with this id. No commit/rollback: the caller owns the
        transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_UPDATE_TOTAL, (total_amount, order_id))
            row = cur.fetchone()
        return OrderResponse.model_validate(row) if row else None

    def get_for_update(
        self, conn: psycopg.Connection, order_id: UUID
    ) -> OrderDetailResponse | None:
        """Return the order with this id plus its items, row-locked, or None.

        The order row is locked FOR UPDATE (migration 001 lock order:
        customer first, then order) and re-read as the latest committed
        version after any lock wait. Items are read unlocked: no
        implemented workflow mutates order_items of an existing order.
        No commit/rollback: the caller owns the transaction.
        """
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
        """SUM(total_amount) over the customer's confirmed orders.

        0.00 when there are none. Excludes delivered orders (their total
        lives in outstanding_balance) and cancelled ones. Money stays
        Decimal end to end (numeric -> Decimal). No commit/rollback:
        the caller owns the transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_SUM_CONFIRMED_TOTALS, (customer_id,))
            row = cur.fetchone()
        return row["confirmed_total"]

    def mark_confirmed(
        self, conn: psycopg.Connection, order_id: UUID
    ) -> OrderResponse | None:
        """Guarded draft -> confirmed transition; return the order, or None.

        Sets status='confirmed' and confirmed_at=now() (database clock)
        only WHERE the current status is 'draft'. None means the row is
        gone or no longer draft: the transition already happened or is
        illegal — the caller decides which. No commit/rollback: the
        caller owns the transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_MARK_CONFIRMED, (order_id,))
            row = cur.fetchone()
        return OrderResponse.model_validate(row) if row else None

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
