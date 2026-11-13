"""PriceRepository live smoke test — NOT run by default.

Manual run (self-cleaning, touches only its own throwaway rows):

    RUN_DB_SMOKE=1 uv run pytest tests/integration/test_price_repository_integration.py -v

Requirements:
- Migration 001 applied to Supabase (price_lists seed 'GENERAL' present)
- .env SUPABASE_DB_URL configured and reachable

Safety:
- Creates one throwaway price list, product, and product price (unique
  code/SKU), never touches existing records. Cleanup deletes its own
  product price first, then price list and product.
"""

import os
from decimal import Decimal
from uuid import uuid4

import pytest

from app.database.connection import transaction
from app.repositories.price import PriceRepository
from app.schemas.price_list import PriceListResponse, ProductPriceResponse

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_SMOKE") != "1",
    reason="live Supabase smoke test: set RUN_DB_SMOKE=1 to run",
)


def test_price_read_roundtrip():
    repo = PriceRepository()
    pl_id = uuid4()
    prod_id = uuid4()
    code = f"SMOKE-PL-{pl_id.hex[:8]}"  # unique, price_lists.code UNIQUE
    sku = f"SMOKE-{prod_id.hex[:8]}"  # unique, products.sku UNIQUE

    inserted = False
    try:
        with transaction() as conn:  # committed setup
            conn.execute(
                "INSERT INTO price_lists (id, code, name) VALUES (%s, %s, %s)",
                (pl_id, code, f"Smoke price list {pl_id.hex[:6]}"),
            )
            conn.execute(
                "INSERT INTO products (id, sku, name, unit) VALUES (%s, %s, %s, %s)",
                (prod_id, sku, f"Smoke Test Product {prod_id.hex[:6]}", "pcs"),
            )
            conn.execute(
                "INSERT INTO product_prices (price_list_id, product_id, unit_price) "
                "VALUES (%s, %s, %s)",
                (pl_id, prod_id, Decimal("1234.50")),
            )
        inserted = True

        by_id = repo.get_price_list_by_id(pl_id)
        assert isinstance(by_id, PriceListResponse)
        assert by_id.code == code

        by_code = repo.get_price_list_by_code(code)
        assert by_code is not None and by_code.id == pl_id

        price = repo.get_product_price(pl_id, prod_id)
        assert isinstance(price, ProductPriceResponse)
        assert price.unit_price == Decimal("1234.50")
        assert price.product_id == prod_id

        # unknown lookups
        assert repo.get_price_list_by_id(uuid4()) is None
        assert repo.get_price_list_by_code("SMOKE-NO-SUCH-CODE") is None
        assert repo.get_product_price(uuid4(), prod_id) is None
        assert repo.get_product_price(pl_id, uuid4()) is None
    finally:
        if inserted:
            with transaction() as conn:  # cleanup own rows only
                conn.execute(
                    "DELETE FROM product_prices WHERE price_list_id = %s AND product_id = %s",
                    (pl_id, prod_id),
                )
                conn.execute("DELETE FROM price_lists WHERE id = %s", (pl_id,))
                conn.execute("DELETE FROM products WHERE id = %s", (prod_id,))
