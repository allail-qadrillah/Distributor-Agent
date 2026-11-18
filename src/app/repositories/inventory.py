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
        with conn.cursor() as cur:
            cur.execute(_GET_BY_PRODUCT_ID, (product_id,))
            row = cur.fetchone()
        return InventoryResponse.model_validate(row) if row else None

    def lock_by_product_ids(
        self, conn: psycopg.Connection, product_ids: Sequence[UUID]
    ) -> list[InventoryResponse]:
        with conn.cursor() as cur:
            cur.execute(_LOCK_BY_PRODUCT_IDS, (list(product_ids),))
            rows = cur.fetchall()
        return [InventoryResponse.model_validate(row) for row in rows]

    def reserve_stock(
        self, conn: psycopg.Connection, product_id: UUID, quantity: int
    ) -> InventoryResponse | None:
        with conn.cursor() as cur:
            cur.execute(_RESERVE_STOCK, (quantity, product_id))
            row = cur.fetchone()
        return InventoryResponse.model_validate(row) if row else None
