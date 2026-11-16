"""Price repository — read-only access to price_lists and product_prices.

Rules (same conventions as other repositories):
- Read-only: no create/update/delete. Price administration and any
  customer-specific resolution (GENERAL fallback etc.) belong to the
  service layer — this module reads exactly what is stored.
- Parameterized SQL only; dict_row; model_validate; None for misses;
  exceptions propagate.

Connection injection (same contract as InventoryRepository/OrderRepository):
- Every method receives the caller's connection and uses it for all
  operations. This module never opens a connection and never manages
  transaction lifecycle: no BEGIN, no COMMIT, no ROLLBACK, no close. The
  caller owns the transaction.
- The connection must use dict_row (app.database.connection.connect and
  transaction() provide it).
"""

from uuid import UUID

import psycopg

from app.schemas.price_list import PriceListResponse, ProductPriceResponse

_PRICE_LIST_COLUMNS = "id, code, name, is_active, created_at, updated_at"
_PRODUCT_PRICE_COLUMNS = (
    "id, price_list_id, product_id, unit_price, created_at, updated_at"
)

_GET_PRICE_LIST_BY_ID = f"SELECT {_PRICE_LIST_COLUMNS} FROM price_lists WHERE id = %s"
_GET_PRICE_LIST_BY_CODE = (
    f"SELECT {_PRICE_LIST_COLUMNS} FROM price_lists WHERE code = %s"
)
_GET_PRODUCT_PRICE = (
    f"SELECT {_PRODUCT_PRICE_COLUMNS} FROM product_prices "
    "WHERE price_list_id = %s AND product_id = %s"
)


class PriceRepository:
    def get_price_list_by_id(
        self, conn: psycopg.Connection, price_list_id: UUID
    ) -> PriceListResponse | None:
        """Return the price list with this id, or None.

        No commit/rollback: the caller owns the transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_GET_PRICE_LIST_BY_ID, (price_list_id,))
            row = cur.fetchone()
        return PriceListResponse.model_validate(row) if row else None

    def get_price_list_by_code(
        self, conn: psycopg.Connection, code: str
    ) -> PriceListResponse | None:
        """Return the price list with this exact code, or None.

        No commit/rollback: the caller owns the transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_GET_PRICE_LIST_BY_CODE, (code,))
            row = cur.fetchone()
        return PriceListResponse.model_validate(row) if row else None

    def get_product_price(
        self, conn: psycopg.Connection, price_list_id: UUID, product_id: UUID
    ) -> ProductPriceResponse | None:
        """Return the price of a product in a price list, or None.

        Exact (price_list_id, product_id) match — mirrors the
        product_prices UNIQUE constraint. No fallback logic here.
        No commit/rollback: the caller owns the transaction.
        """
        with conn.cursor() as cur:
            cur.execute(_GET_PRODUCT_PRICE, (price_list_id, product_id))
            row = cur.fetchone()
        return ProductPriceResponse.model_validate(row) if row else None
