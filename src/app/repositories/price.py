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
        with conn.cursor() as cur:
            cur.execute(_GET_PRICE_LIST_BY_ID, (price_list_id,))
            row = cur.fetchone()
        return PriceListResponse.model_validate(row) if row else None

    def get_price_list_by_code(
        self, conn: psycopg.Connection, code: str
    ) -> PriceListResponse | None:
        with conn.cursor() as cur:
            cur.execute(_GET_PRICE_LIST_BY_CODE, (code,))
            row = cur.fetchone()
        return PriceListResponse.model_validate(row) if row else None

    def get_product_price(
        self, conn: psycopg.Connection, price_list_id: UUID, product_id: UUID
    ) -> ProductPriceResponse | None:
        with conn.cursor() as cur:
            cur.execute(_GET_PRODUCT_PRICE, (price_list_id, product_id))
            row = cur.fetchone()
        return ProductPriceResponse.model_validate(row) if row else None
