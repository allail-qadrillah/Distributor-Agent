"""Inventory repository — read-only database access for the inventory table.

Read-only by design: stock reservation, release, physical reduction and
row creation happen only in the business service layer (inside
transactions with row locks — see migration 001 header). This module
never writes and never computes available_stock: it reads the generated
column straight from the database.
"""

from uuid import UUID

from app.database.connection import get_connection
from app.schemas.inventory import InventoryResponse

# Exactly the InventoryResponse columns, including the generated
# available_stock (= on_hand_stock - reserved_stock, computed by Postgres)
_COLUMNS = "id, product_id, on_hand_stock, reserved_stock, available_stock, updated_at"
_GET_BY_PRODUCT_ID = f"SELECT {_COLUMNS} FROM inventory WHERE product_id = %s"


class InventoryRepository:
    def get_by_product_id(self, product_id: UUID) -> InventoryResponse | None:
        """Return the inventory row for this product, or None.

        available_stock comes from the database generated column — never
        recalculated in Python.
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_GET_BY_PRODUCT_ID, (product_id,))
                row = cur.fetchone()
        return InventoryResponse.model_validate(row) if row else None
