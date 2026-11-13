"""Product repository — database access for products and product aliases.

Rules (same conventions as CustomerRepository):
- Database operations only. No business rules: no pricing, no inventory,
  no order workflows, no LLM, no API.
- Parameterized SQL only (never string-formatted).
- Returns Pydantic schemas, None for missing single records, lists for
  searches; never raw cursors.
- Psycopg exceptions propagate (never swallowed) — user-safe mapping is
  the API layer's job.
"""

from uuid import UUID

from app.database.connection import get_connection
from app.schemas.product import ProductResponse

# Exactly the ProductResponse columns — nothing more
_COLUMNS = "p.id, p.sku, p.name, p.unit, p.is_active, p.created_at, p.updated_at"
_GET_BY_ID = f"SELECT {_COLUMNS.replace('p.', '')} FROM products WHERE id = %s"
_GET_BY_SKU = f"SELECT {_COLUMNS.replace('p.', '')} FROM products WHERE sku = %s"
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
    """Escape LIKE/ILIKE wildcards so user input cannot broaden the match."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ProductRepository:
    def get_by_id(self, product_id: UUID) -> ProductResponse | None:
        """Return the product with this id, or None."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_GET_BY_ID, (product_id,))
                row = cur.fetchone()
        return ProductResponse.model_validate(row) if row else None

    def get_by_sku(self, sku: str) -> ProductResponse | None:
        """Return the product with this exact sku, or None."""
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_GET_BY_SKU, (sku,))
                row = cur.fetchone()
        return ProductResponse.model_validate(row) if row else None

    def search_by_name_or_alias(self, query: str) -> list[ProductResponse]:
        """Case-insensitive partial search over product names and aliases.

        Unique, active products only; empty list when nothing matches.
        """
        pattern = f"%{_escape_like(query)}%"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SEARCH, (pattern, pattern))
                rows = cur.fetchall()
        return [ProductResponse.model_validate(row) for row in rows]
