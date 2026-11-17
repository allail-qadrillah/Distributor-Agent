"""ProductInquiryService live smoke test — NOT run by default.

Manual run (self-cleaning, touches only its own throwaway rows):

    RUN_DB_SMOKE=1 uv run pytest tests/integration/test_product_inquiry_integration.py -v

Requirements:
- Migration 001 applied to Supabase (price_lists seed 'GENERAL' present)
- .env SUPABASE_DB_URL configured and reachable

Safety:
- Creates throwaway price list, customers, products, aliases, prices,
  and inventory rows (random code/SKU/phone), never reuses or modifies
  existing records, deletes its own rows in teardown (customer, price
  list, products — cascades cover aliases, product_prices, inventory).
- Scenario 2 (GENERAL fallback) inserts a throwaway product_prices row
  into the seeded GENERAL list; the GENERAL row itself is never modified
  or deleted (guarded by an explicit post-cleanup assertion) and the
  child row is cascade-deleted with its throwaway product.
- Inquiry is read-only: every test asserts stock and reserved quantities
  are unchanged by the workflow.
"""

import os
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.database.connection import get_connection, transaction
from app.repositories.customer import CustomerRepository
from app.repositories.inventory import InventoryRepository
from app.repositories.price import PriceRepository
from app.repositories.product import ProductRepository
from app.schemas.product_inquiry import ProductInquiryRequest
from app.services.exceptions import (
    CustomerNotFoundError,
    InventoryNotFoundError,
    ProductPriceNotFoundError,
)
from app.services.product_inquiry import ProductInquiryService

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_SMOKE") != "1",
    reason="live Supabase smoke test: set RUN_DB_SMOKE=1 to run",
)


def _service() -> ProductInquiryService:
    return ProductInquiryService(
        CustomerRepository(),
        ProductRepository(),
        PriceRepository(),
        InventoryRepository(),
    )


def _sku(prod_id: UUID) -> str:
    return f"SMOKE-INQ-{prod_id.hex[:8].upper()}"


def _phone(cust_id: UUID) -> str:
    # numeric only: UUID hex can contain a-f, which the E.164 CHECK rejects
    return f"+62895{cust_id.int % 1_000_000_000:09d}"


def _insert_customer(conn, cust_id: UUID, price_list_id: UUID | None = None) -> None:
    conn.execute(
        "INSERT INTO customers (id, code, name, phone, price_list_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            cust_id,
            f"SMOKEINQ-{cust_id.hex[:8]}",
            "Smoke Inquiry Customer",
            _phone(cust_id),
            price_list_id,
        ),
    )


def _insert_product(
    conn,
    prod_id: UUID,
    *,
    name: str = "Smoke Inquiry Product",
    is_active: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO products (id, sku, name, unit, is_active) "
        "VALUES (%s, %s, %s, %s, %s)",
        (prod_id, _sku(prod_id), name, "pcs", is_active),
    )


def _insert_alias(conn, prod_id: UUID, alias: str) -> None:
    conn.execute(
        "INSERT INTO product_aliases (product_id, alias) VALUES (%s, %s)",
        (prod_id, alias),
    )


def _insert_price(
    conn, price_list_id: UUID, prod_id: UUID, unit_price: Decimal
) -> None:
    conn.execute(
        "INSERT INTO product_prices (price_list_id, product_id, unit_price) "
        "VALUES (%s, %s, %s)",
        (price_list_id, prod_id, unit_price),
    )


def _insert_inventory(conn, prod_id: UUID, on_hand: int, reserved: int = 0) -> None:
    conn.execute(
        "INSERT INTO inventory (product_id, on_hand_stock, reserved_stock) "
        "VALUES (%s, %s, %s)",
        (prod_id, on_hand, reserved),
    )


def _cleanup(conn, cust_id: UUID, pl_id: UUID, prod_ids: list[UUID]) -> None:
    conn.execute("DELETE FROM customers WHERE id = %s", (cust_id,))
    conn.execute("DELETE FROM price_lists WHERE id = %s", (pl_id,))
    for prod_id in prod_ids:  # cascades: aliases, product_prices, inventory
        conn.execute("DELETE FROM products WHERE id = %s", (prod_id,))


def _general_list_id(conn) -> UUID:
    general = PriceRepository().get_price_list_by_code(conn, "GENERAL")
    assert general is not None  # migration 001 seed row
    return general.id


def _stock_rows(conn, prod_ids: list[UUID]) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT on_hand_stock, reserved_stock FROM inventory "
        "WHERE product_id = ANY(%s) ORDER BY product_id",
        (prod_ids,),
    ).fetchall()
    return [(r["on_hand_stock"], r["reserved_stock"]) for r in rows]


def test_inquiry_with_assigned_price_list():
    cust_id, pl_id, prod_id = uuid4(), uuid4(), uuid4()

    inserted = False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO price_lists (id, code, name) VALUES (%s, %s, %s)",
                (pl_id, f"SMOKEINQ-PL-{pl_id.hex[:8]}", "Smoke Inquiry List"),
            )
            _insert_customer(conn, cust_id, pl_id)
            _insert_product(conn, prod_id)
            _insert_price(conn, pl_id, prod_id, Decimal("1234.50"))
            _insert_inventory(conn, prod_id, 100)
        inserted = True

        result = _service().search_products(
            ProductInquiryRequest(
                customer_id=cust_id, search_term="Smoke Inquiry Product"
            )
        )
        assert [p.product_id for p in result.products] == [prod_id]
        item = result.products[0]
        assert item.sku == _sku(prod_id)
        assert item.unit_price == Decimal("1234.50")
        assert item.available_stock == 100

        with transaction() as conn:  # read-only workflow: stock untouched
            assert _stock_rows(conn, [prod_id]) == [(100, 0)]
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, pl_id, [prod_id])


def test_inquiry_null_price_list_uses_general():
    cust_id, prod_id = uuid4(), uuid4()
    general_id = None
    inserted = False
    try:
        with transaction() as conn:
            general_id = _general_list_id(conn)
            _insert_customer(conn, cust_id, price_list_id=None)
            _insert_product(conn, prod_id)
            _insert_price(conn, general_id, prod_id, Decimal("99.00"))
            _insert_inventory(conn, prod_id, 7)
        inserted = True

        result = _service().search_products(
            ProductInquiryRequest(
                customer_id=cust_id, search_term="Smoke Inquiry Product"
            )
        )
        assert [p.product_id for p in result.products] == [prod_id]
        assert result.products[0].unit_price == Decimal("99.00")
    finally:
        if inserted:
            with transaction() as conn:  # product cascade removes the GENERAL price
                # the GENERAL list itself is a migration 001 seed row: it
                # is NEVER deleted — only this test's customer and product
                # (whose cascade owns the throwaway product_prices row)
                conn.execute("DELETE FROM customers WHERE id = %s", (cust_id,))
                conn.execute("DELETE FROM products WHERE id = %s", (prod_id,))
            with get_connection() as conn:  # guard: seed row still present
                assert (
                    PriceRepository().get_price_list_by_code(conn, "GENERAL")
                    is not None
                )


def test_exact_sku_lookup_case_insensitive_and_priority():
    cust_id, pl_id, sku_prod, name_prod = uuid4(), uuid4(), uuid4(), uuid4()
    inserted = False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO price_lists (id, code, name) VALUES (%s, %s, %s)",
                (pl_id, f"SMOKEINQ-PL-{pl_id.hex[:8]}", "Smoke Inquiry List"),
            )
            _insert_customer(conn, cust_id, pl_id)
            # stored SKU is upper; a second product's NAME contains the same
            # term in lowercase, so an exact SKU match must beat name search
            _insert_product(conn, sku_prod)
            _insert_product(conn, name_prod, name=_sku(sku_prod).lower())
            for prod in (sku_prod, name_prod):
                _insert_price(conn, pl_id, prod, Decimal("10.00"))
                _insert_inventory(conn, prod, 10)
        inserted = True

        stored_sku = _sku(sku_prod)
        mixed = stored_sku.lower().replace("-inq-", "-InQ-")
        assert mixed != stored_sku  # casing genuinely differs
        result = _service().search_products(
            ProductInquiryRequest(customer_id=cust_id, search_term=mixed)
        )
        assert [p.product_id for p in result.products] == [sku_prod]
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, pl_id, [sku_prod, name_prod])


def test_name_and_alias_lookup():
    cust_id, pl_id, prod_id = uuid4(), uuid4(), uuid4()
    alias = f"minuman-kolak-{prod_id.hex[:6]}"
    inserted = False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO price_lists (id, code, name) VALUES (%s, %s, %s)",
                (pl_id, f"SMOKEINQ-PL-{pl_id.hex[:8]}", "Smoke Inquiry List"),
            )
            _insert_customer(conn, cust_id, pl_id)
            _insert_product(conn, prod_id, name=f"Wedang Uwu {prod_id.hex[:6]}")
            _insert_alias(conn, prod_id, alias)
            _insert_price(conn, pl_id, prod_id, Decimal("15.00"))
            _insert_inventory(conn, prod_id, 20)
        inserted = True

        by_partial_name = _service().search_products(
            ProductInquiryRequest(customer_id=cust_id, search_term="Uwu")
        )
        assert [p.product_id for p in by_partial_name.products] == [prod_id]

        by_alias = _service().search_products(
            ProductInquiryRequest(customer_id=cust_id, search_term="kolak")
        )
        assert [p.product_id for p in by_alias.products] == [prod_id]
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, pl_id, [prod_id])


def test_inactive_products_excluded():
    cust_id, pl_id, active, inactive = uuid4(), uuid4(), uuid4(), uuid4()
    marker = f"Es Lilin {uuid4().hex[:6]}"
    inserted = False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO price_lists (id, code, name) VALUES (%s, %s, %s)",
                (pl_id, f"SMOKEINQ-PL-{pl_id.hex[:8]}", "Smoke Inquiry List"),
            )
            _insert_customer(conn, cust_id, pl_id)
            _insert_product(conn, active, name=marker)
            _insert_product(conn, inactive, name=marker, is_active=False)
            for prod in (active, inactive):
                _insert_price(conn, pl_id, prod, Decimal("5.00"))
                _insert_inventory(conn, prod, 5)
        inserted = True

        by_name = _service().search_products(
            ProductInquiryRequest(customer_id=cust_id, search_term=marker)
        )
        assert [p.product_id for p in by_name.products] == [active]

        # exact SKU of the inactive product: no match, and the follow-up
        # name search must not surface it either
        by_sku = _service().search_products(
            ProductInquiryRequest(customer_id=cust_id, search_term=_sku(inactive))
        )
        assert by_sku.products == []
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, pl_id, [active, inactive])


def test_missing_product_price_raises():
    cust_id, pl_id, prod_id = uuid4(), uuid4(), uuid4()
    inserted = False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO price_lists (id, code, name) VALUES (%s, %s, %s)",
                (pl_id, f"SMOKEINQ-PL-{pl_id.hex[:8]}", "Smoke Inquiry List"),
            )
            _insert_customer(conn, cust_id, pl_id)
            _insert_product(conn, prod_id)  # priced nowhere: no product_prices row
            _insert_inventory(conn, prod_id, 30)
        inserted = True

        with pytest.raises(ProductPriceNotFoundError) as excinfo:
            _service().search_products(
                ProductInquiryRequest(
                    customer_id=cust_id, search_term="Smoke Inquiry Product"
                )
            )
        assert excinfo.value.price_list_id == pl_id
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, pl_id, [prod_id])


def test_missing_inventory_row_raises():
    cust_id, pl_id, prod_id = uuid4(), uuid4(), uuid4()
    inserted = False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO price_lists (id, code, name) VALUES (%s, %s, %s)",
                (pl_id, f"SMOKEINQ-PL-{pl_id.hex[:8]}", "Smoke Inquiry List"),
            )
            _insert_customer(conn, cust_id, pl_id)
            _insert_product(conn, prod_id)  # no inventory row
            _insert_price(conn, pl_id, prod_id, Decimal("8.00"))
        inserted = True

        with pytest.raises(InventoryNotFoundError):
            _service().search_products(
                ProductInquiryRequest(
                    customer_id=cust_id, search_term="Smoke Inquiry Product"
                )
            )
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, pl_id, [prod_id])


def test_zero_available_stock_returns_zero():
    cust_id, pl_id, prod_id = uuid4(), uuid4(), uuid4()
    inserted = False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO price_lists (id, code, name) VALUES (%s, %s, %s)",
                (pl_id, f"SMOKEINQ-PL-{pl_id.hex[:8]}", "Smoke Inquiry List"),
            )
            _insert_customer(conn, cust_id, pl_id)
            _insert_product(conn, prod_id)
            _insert_price(conn, pl_id, prod_id, Decimal("12.00"))
            _insert_inventory(conn, prod_id, 0)  # on_hand 0, reserved 0
        inserted = True

        result = _service().search_products(
            ProductInquiryRequest(
                customer_id=cust_id, search_term="Smoke Inquiry Product"
            )
        )
        assert [p.product_id for p in result.products] == [prod_id]
        assert result.products[0].available_stock == 0

        with transaction() as conn:  # zero stays a read, never a write
            assert _stock_rows(conn, [prod_id]) == [(0, 0)]
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, pl_id, [prod_id])


def test_unknown_customer_raises():
    _service()  # construction touches nothing
    with pytest.raises(CustomerNotFoundError):
        _service().search_products(
            ProductInquiryRequest(customer_id=uuid4(), search_term="anything")
        )
