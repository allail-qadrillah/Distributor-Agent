from uuid import UUID

import psycopg

from app.schemas.product import ProductResponse

# Exactly the ProductResponse columns — nothing more
_COLUMNS = "p.id, p.sku, p.name, p.unit, p.is_active, p.created_at, p.updated_at"
_GET_BY_ID = f"SELECT {_COLUMNS.replace('p.', '')} FROM products WHERE id = %s"
_GET_BY_SKU = f"SELECT {_COLUMNS.replace('p.', '')} FROM products WHERE sku = %s"
# Exact case-insensitive SKU match, active products only. lower() on both
# sides (never pre-lowered in Python): case folding is the database's job.
# products.sku UNIQUE is case-sensitive, so different casings are distinct
# rows and can all match — hence fetchall and a list return.
_SEARCH_BY_EXACT_SKU = f"""
SELECT {_COLUMNS}
FROM products p
WHERE p.is_active = true AND lower(p.sku) = lower(%s)
ORDER BY p.sku
"""
# DISTINCT: one row per product even if name AND several aliases match.
# LEFT JOIN: name-only matches still returned for products without aliases.
# ILIKE + ESCAPE: case-insensitive partial match with escaped wildcards.
_SEARCH = f"""
SELECT DISTINCT {_COLUMNS}
FROM products p
LEFT JOIN product_aliases pa ON pa.product_id = p.id
WHERE p.is_active = true
  AND (p.name ILIKE %s ESCAPE '\\' OR pa.alias ILIKE %s ESCAPE '\\')
ORDER BY p.sku
"""


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ProductRepository:
    def get_by_id(
        self, conn: psycopg.Connection, product_id: UUID
    ) -> ProductResponse | None:
        with conn.cursor() as cur:
            cur.execute(_GET_BY_ID, (product_id,))
            row = cur.fetchone()
        return ProductResponse.model_validate(row) if row else None

    def get_by_sku(self, conn: psycopg.Connection, sku: str) -> ProductResponse | None:
        with conn.cursor() as cur:
            cur.execute(_GET_BY_SKU, (sku,))
            row = cur.fetchone()
        return ProductResponse.model_validate(row) if row else None

    def search_by_exact_sku(
        self, conn: psycopg.Connection, sku: str
    ) -> list[ProductResponse]:
        with conn.cursor() as cur:
            cur.execute(_SEARCH_BY_EXACT_SKU, (sku,))
            rows = cur.fetchall()
        return [ProductResponse.model_validate(row) for row in rows]

    def search_by_name_or_alias(
        self, conn: psycopg.Connection, query: str
    ) -> list[ProductResponse]:
        pattern = f"%{_escape_like(query)}%"
        with conn.cursor() as cur:
            cur.execute(_SEARCH, (pattern, pattern))
            rows = cur.fetchall()
        return [ProductResponse.model_validate(row) for row in rows]
