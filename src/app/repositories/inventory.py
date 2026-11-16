"""Inventory repository — database access to the inventory table.

Business decisions (whether stock suffices, whether to reserve) live in
the service layer; this module owns the SQL: row locking in ascending
product_id order (migration 001 deadlock avoidance) and the
reserved_stock increment. on_hand_stock is never written here — physical
reduction is the delivery workflow's job. available_stock stays a
database generated column: this module never computes it.

Rules (same conventions as other repositories):
- Database operations only. No business rules.
- Parameterized SQL only (never string-formatted).
- Returns Pydantic schemas or None; never raw cursors.
- Psycopg exceptions propagate to the caller (never swallowed) — mapping
  them to user-safe responses is the API layer's job.

Connection injection (same contract as OrderRepository):
- Every method receives the caller's connection and uses it for all
  operations. This module never opens a connection and never manages
  transaction lifecycle: no BEGIN, no COMMIT, no ROLLBACK, no close. The
  caller owns the transaction.
- The connection must use dict_row (app.database.connection.connect and
  transaction() provide it).
"""

from collections.abc import Sequence
from uuid import UUID

import psycopg

from app.schemas.inventory import InventoryResponse

# Exactly the InventoryResponse columns, including the generated
# available_stock (= on_hand_stock - reserved_stock, computed by Postgres)
_COLUMNS = "id, product_id, on_hand_stock, reserved_stock, available_stock, updated_at"
_GET_BY_PRODUCT_ID = f"SELECT {_COLUMNS} FROM inventory WHERE product_id = %s"
# One statement locks all requested rows; ORDER BY product_id makes the
# lock acquisition order deterministic (migration 001 deadlock rule).
_LOCK_BY_PRODUCT_IDS = f"""
SELECT {_COLUMNS} FROM inventory WHERE product_id = ANY(%s) ORDER BY product_id FOR UPDATE
"""
# Increment in SQL, never read-modify-write in Python. The DB CHECK
# reserved_stock <= on_hand_stock backstops buggy callers.
_RESERVE_STOCK = f"""
UPDATE inventory SET reserved_stock = reserved_stock + %s WHERE product_id = %s
RETURNING {_COLUMNS}
"""


class InventoryRepository:
    def get_by_product_id(
        self, conn: psycopg.Connection, product_id: UUID
    ) -> InventoryResponse | None:
        """Return the inventory row for this product, or None.

        available_stock comes from the database generated column — never
        recalculated in Python. No commit/rollback: the caller owns the
        transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_GET_BY_PRODUCT_ID, (product_id,))
            row = cur.fetchone()
        return InventoryResponse.model_validate(row) if row else None

    def lock_by_product_ids(
        self, conn: psycopg.Connection, product_ids: Sequence[UUID]
    ) -> list[InventoryResponse]:
        """Lock and return the inventory rows for these products.

        One SELECT ... FOR UPDATE: all rows are locked in a single
        statement, acquired in ascending product_id order (migration 001
        deadlock-avoidance rule — callers must already hold the customer
        and order locks). Products without an inventory row are simply
        absent from the result; detecting missing rows is the service's
        job. No commit/rollback: the caller owns the transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_LOCK_BY_PRODUCT_IDS, (list(product_ids),))
            rows = cur.fetchall()
        return [InventoryResponse.model_validate(row) for row in rows]

    def reserve_stock(
        self, conn: psycopg.Connection, product_id: UUID, quantity: int
    ) -> InventoryResponse | None:
        """Add quantity to reserved_stock; return the updated row, or None.

        reserved_stock is incremented in SQL; on_hand_stock is never
        touched. The DB CHECK reserved_stock <= on_hand_stock is the
        backstop: a violating update raises and the caller's transaction
        rolls back. None means no inventory row for this product. No
        commit/rollback: the caller owns the transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_RESERVE_STOCK, (quantity, product_id))
            row = cur.fetchone()
        return InventoryResponse.model_validate(row) if row else None
