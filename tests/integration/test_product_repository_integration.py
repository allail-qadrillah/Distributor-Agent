"""ProductRepository live smoke test — NOT run by default.

Manual run (self-cleaning, touches only its own throwaway rows):

    RUN_DB_SMOKE=1 uv run pytest tests/integration/test_product_repository_integration.py -v

Requirements:
- Migration 001 applied to Supabase
- .env SUPABASE_DB_URL configured and reachable

Safety:
- Creates one throwaway product and one throwaway alias (random SKU/alias),
  never reuses or modifies existing records, deletes its own rows in
  teardown (alias explicitly, then product — cascade also covers it).
- Every repository call receives the connection from transaction() —
  the repository itself never opens, commits, or rolls back.
"""

import os
from uuid import uuid4

import pytest

from app.database.connection import transaction
from app.repositories.product import ProductRepository
from app.schemas.product import ProductResponse

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_SMOKE") != "1",
    reason="live Supabase smoke test: set RUN_DB_SMOKE=1 to run",
)


def test_product_lookup_and_search_roundtrip():
    repo = ProductRepository()
    prod_id = uuid4()
    sku = f"SMOKE-{prod_id.hex[:8]}"  # unique, products.sku UNIQUE
    name = f"Smoke Test Product {prod_id.hex[:6]}"  # non-blank text
    alias = f"smokealias {prod_id.hex[:8]}"  # unique, case-insensitive global

    inserted = False
    try:
        with transaction() as conn:  # insert throwaway product + alias
            conn.execute(
                "INSERT INTO products (id, sku, name, unit) VALUES (%s, %s, %s, %s)",
                (prod_id, sku, name, "pcs"),
            )
            conn.execute(
                "INSERT INTO product_aliases (product_id, alias) VALUES (%s, %s)",
                (prod_id, alias),
            )
        inserted = True

        with transaction() as conn:  # caller-owned read transaction
            by_id = repo.get_by_id(conn, prod_id)
            assert isinstance(by_id, ProductResponse)
            assert by_id.sku == sku and by_id.unit == "pcs"

            by_sku = repo.get_by_sku(conn, sku)
            assert by_sku is not None and by_sku.id == prod_id

            by_name = repo.search_by_name_or_alias(conn, name)
            assert any(p.id == prod_id for p in by_name)

            by_alias = repo.search_by_name_or_alias(
                conn, f"smokealias {prod_id.hex[:4]}"
            )
            assert any(p.id == prod_id for p in by_alias)

            assert repo.get_by_id(conn, uuid4()) is None
            assert repo.get_by_sku(conn, f"SMOKE-UNKNOWN-{prod_id.hex[:8]}") is None
            assert repo.search_by_name_or_alias(conn, "zzzz-no-such-product") == []
    finally:
        if inserted:
            with transaction() as conn:  # cleanup own rows only
                conn.execute(
                    "DELETE FROM product_aliases WHERE product_id = %s", (prod_id,)
                )
                conn.execute("DELETE FROM products WHERE id = %s", (prod_id,))
