"""Inventory repository — read-only database access to the inventory table.

Read-only by design: stock reservation, release, physical reduction and
row creation happen only in the business service layer (inside
transactions with row locks — see migration 001 header). This module
never writes and never computes available_stock: it reads the generated
column straight from the database.

Rules (same conventions as other repositories):
- Database operations only. No business rules.
- Parameterized SQL only (never string-formatted).
- Returns Pydantic schemas or None; never raw cursors.
- Psycopg exceptions propagate to the caller (never swallowed) — mapping
  them to user-safe responses is the API layer's job.

Connection injection (same contract as OrderRepository):
- The method receives the caller's connection and uses it for all
  operations. This module never opens a connection and never manages
  transaction lifecycle: no BEGIN, no COMMIT, no ROLLBACK, no close. The
  caller owns the transaction.
- The connection must use dict_row (app.database.connection.connect and
  transaction() provide it).
"""

from uuid import UUID

import psycopg

from app.schemas.inventory import InventoryResponse

# Exactly the InventoryResponse columns, including the generated
# available_stock (= on_hand_stock - reserved_stock, computed by Postgres)
_COLUMNS = "id, product_id, on_hand_stock, reserved_stock, available_stock, updated_at"
_GET_BY_PRODUCT_ID = f"SELECT {_COLUMNS} FROM inventory WHERE product_id = %s"


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
