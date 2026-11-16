"""OrderService live smoke test — NOT run by default.

Manual run (self-cleaning, touches only its own throwaway rows):

    RUN_DB_SMOKE=1 uv run pytest tests/integration/test_order_service_integration.py -v

Requirements:
- Migration 001 applied to Supabase
- .env SUPABASE_DB_URL configured and reachable

Safety:
- Creates throwaway price list, customer, products, prices, and
  inventory rows (random code/SKU/phone), never reuses or modifies
  existing records, deletes its own rows in teardown (order_items,
  orders, price list, products, customer — FK-safe order; cascades
  cover product_prices and inventory).
- Draft orders reserve no inventory and consume no credit (migration 001
  business rules), asserted explicitly here.
- Rollback check: update_total_amount is patched to raise inside the
  workflow's transaction; the complete draft must disappear.
- Confirm tests additionally verify: all-or-nothing reservation
  (on_hand untouched, reserved incremented), credit exposure boundaries,
  guarded status transition, and customer-lock serialization of
  concurrent confirmations (threads).
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest

from app.database.connection import transaction
from app.repositories.customer import CustomerRepository
from app.repositories.inventory import InventoryRepository
from app.repositories.order import OrderRepository
from app.repositories.price import PriceRepository
from app.repositories.product import ProductRepository
from app.schemas.inventory import InventoryResponse
from app.schemas.order import (
    OrderCreate,
    OrderDetailResponse,
    OrderItemCreate,
    OrderStatus,
)
from app.services.exceptions import (
    CreditLimitExceededError,
    CreditLimitNotConfiguredError,
    InsufficientStockError,
    InvalidOrderStatusError,
    InventoryNotFoundError,
    ProductPriceNotFoundError,
)
from app.services.order import GENERAL_PRICE_LIST_CODE, OrderService

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_SMOKE") != "1",
    reason="live Supabase smoke test: set RUN_DB_SMOKE=1 to run",
)


def _service() -> OrderService:
    return OrderService(
        CustomerRepository(),
        ProductRepository(),
        PriceRepository(),
        OrderRepository(),
        InventoryRepository(),
    )


def _insert_customer(
    conn,
    cust_id: UUID,
    code: str,
    phone: str,
    price_list_id: UUID | None = None,
    credit_limit: Decimal | None = Decimal("0.00"),
) -> None:
    conn.execute(
        "INSERT INTO customers (id, code, name, phone, price_list_id, credit_limit) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (cust_id, code, "Smoke Service Customer", phone, price_list_id, credit_limit),
    )


def _insert_product(conn, product_id: UUID, unit: str = "pcs") -> None:
    conn.execute(
        "INSERT INTO products (id, sku, name, unit) VALUES (%s, %s, %s, %s)",
        (
            product_id,
            f"SMOKE-SVC-{product_id.hex[:8]}",
            f"Smoke Product {product_id.hex[:4]}",
            unit,
        ),
    )


def _insert_inventory(conn, product_id: UUID, on_hand: int, reserved: int = 0) -> None:
    conn.execute(
        "INSERT INTO inventory (product_id, on_hand_stock, reserved_stock) "
        "VALUES (%s, %s, %s)",
        (product_id, on_hand, reserved),
    )


def _insert_price(
    conn, price_list_id: UUID, product_id: UUID, unit_price: Decimal
) -> None:
    conn.execute(
        "INSERT INTO product_prices (price_list_id, product_id, unit_price) "
        "VALUES (%s, %s, %s)",
        (price_list_id, product_id, unit_price),
    )


def _cleanup(
    conn, cust_id: UUID, product_ids: list[UUID], price_list_id: UUID | None
) -> None:
    # own rows only, FK-safe order (cascades remove product_prices/inventory);
    # the customer must go before its price list (customers RESTRICT)
    conn.execute(
        "DELETE FROM order_items WHERE order_id IN "
        "(SELECT id FROM orders WHERE customer_id = %s)",
        (cust_id,),
    )
    conn.execute("DELETE FROM orders WHERE customer_id = %s", (cust_id,))
    conn.execute("DELETE FROM products WHERE id = ANY(%s)", (product_ids,))
    conn.execute("DELETE FROM customers WHERE id = %s", (cust_id,))
    if price_list_id is not None:
        conn.execute("DELETE FROM price_lists WHERE id = %s", (price_list_id,))


def _order_count(conn, cust_id: UUID) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM orders WHERE customer_id = %s", (cust_id,)
    ).fetchone()
    return row["n"]


def _item_count(conn, product_ids: list[UUID]) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM order_items WHERE product_id = ANY(%s)",
        (product_ids,),
    ).fetchone()
    return row["n"]


def _assert_inventory_unchanged(conn, product_id: UUID, on_hand: int) -> None:
    inv = InventoryRepository().get_by_product_id(conn, product_id)
    assert isinstance(inv, InventoryResponse)
    assert inv.on_hand_stock == on_hand
    assert inv.reserved_stock == 0  # drafts never reserve
    assert inv.available_stock == on_hand


def _assert_balance_unchanged(conn, cust_id: UUID) -> None:
    customer = CustomerRepository().get_by_id(conn, cust_id)
    assert customer is not None
    assert customer.outstanding_balance == Decimal("0.00")  # drafts consume no credit


def _assert_inventory_reserved(
    conn, product_id: UUID, on_hand: int, reserved: int
) -> None:
    inv = InventoryRepository().get_by_product_id(conn, product_id)
    assert isinstance(inv, InventoryResponse)
    assert inv.on_hand_stock == on_hand  # confirmation never touches physical stock
    assert inv.reserved_stock == reserved
    assert inv.available_stock == on_hand - reserved  # generated column agrees


def _seed_confirm_world(
    conn,
    cust_id: UUID,
    p1: UUID,
    p2: UUID,
    *,
    credit_limit: Decimal | None,
    p1_stock: int = 100,
    p2_stock: int = 50,
) -> None:
    """Throwaway parents for a two-item confirm: customer + GENERAL prices."""
    general = PriceRepository().get_price_list_by_code(conn, GENERAL_PRICE_LIST_CODE)
    assert general is not None  # migration 001 seed row
    _insert_customer(
        conn,
        cust_id,
        f"SMOKESVC-{cust_id.hex[:8]}",
        f"+62893{cust_id.int % 1_000_000_000:09d}",  # E.164: numeric only
        credit_limit=credit_limit,
    )
    _insert_product(conn, p1)
    _insert_product(conn, p2)
    _insert_inventory(conn, p1, p1_stock)
    _insert_inventory(conn, p2, p2_stock)
    _insert_price(conn, general.id, p1, Decimal("150.00"))
    _insert_price(conn, general.id, p2, Decimal("2500.00"))


def _two_item_draft(cust_id: UUID, p1: UUID, p2: UUID) -> OrderCreate:
    # totals 2 * 150.00 + 1 * 2500.00 = 2800.00
    return OrderCreate(
        customer_id=cust_id,
        notes="confirm smoke",
        items=[
            OrderItemCreate(product_id=p1, quantity=2),
            OrderItemCreate(product_id=p2, quantity=1),
        ],
    )


def test_create_draft_order_general_price_list():
    cust_id = uuid4()
    p1, p2 = uuid4(), uuid4()
    code = f"SMOKESVC-{cust_id.hex[:8]}"
    # numeric-only: UUID hex can contain a-f, which the E.164 CHECK rejects.
    phone = f"+62897{cust_id.int % 1_000_000_000:09d}"

    inserted = False
    order_id = None
    try:
        with transaction() as conn:  # throwaway FK parents + prices + stock
            general = PriceRepository().get_price_list_by_code(
                conn, GENERAL_PRICE_LIST_CODE
            )
            assert general is not None  # migration 001 seed row
            _insert_customer(conn, cust_id, code, phone)  # price_list_id NULL
            _insert_product(conn, p1)
            _insert_product(conn, p2, "carton")
            _insert_inventory(conn, p1, 100)
            _insert_inventory(conn, p2, 50)
            _insert_price(conn, general.id, p1, Decimal("150.00"))
            _insert_price(conn, general.id, p2, Decimal("2500.00"))
        inserted = True  # only after the parent transaction committed

        detail = _service().create_draft_order(
            OrderCreate(
                customer_id=cust_id,
                notes="service smoke draft",
                items=[
                    OrderItemCreate(product_id=p1, quantity=2),
                    OrderItemCreate(product_id=p2, quantity=1),
                ],
            )
        )
        order_id = detail.id

        assert isinstance(detail, OrderDetailResponse)
        assert detail.customer_id == cust_id
        assert re.fullmatch(r"ORD-\d{8}", detail.order_number)
        assert detail.status is OrderStatus.DRAFT
        assert detail.notes == "service smoke draft"
        assert detail.created_at is not None
        assert detail.updated_at is not None  # total update fired trg_orders_upd
        assert detail.confirmed_at is None and detail.delivered_at is None

        by_product = {i.product_id: i for i in detail.items}
        # backend-resolved snapshots + database-generated subtotals
        assert by_product[p1].unit_price == Decimal("150.00")
        assert by_product[p1].quantity == 2
        assert by_product[p1].subtotal == Decimal("300.00")
        assert by_product[p2].unit_price == Decimal("2500.00")
        assert by_product[p2].subtotal == Decimal("2500.00")
        assert detail.total_amount == Decimal("2800.00")

        with transaction() as conn:  # fresh read after commit
            persisted = OrderRepository().get_by_id(conn, order_id)
            assert persisted is not None
            assert persisted.status is OrderStatus.DRAFT
            assert persisted.total_amount == Decimal("2800.00")
            assert len(persisted.items) == 2
            # draft creation is side-effect free (migration 001 rules)
            _assert_balance_unchanged(conn, cust_id)
            _assert_inventory_unchanged(conn, p1, 100)
            _assert_inventory_unchanged(conn, p2, 50)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_create_draft_order_customer_price_list_wins_over_general():
    cust_id = uuid4()
    p1 = uuid4()
    list_id = uuid4()
    code = f"SMOKESVC-{cust_id.hex[:8]}"
    phone = f"+62896{cust_id.int % 1_000_000_000:09d}"

    inserted = False
    try:
        with transaction() as conn:
            general = PriceRepository().get_price_list_by_code(
                conn, GENERAL_PRICE_LIST_CODE
            )
            assert general is not None
            conn.execute(
                "INSERT INTO price_lists (id, code, name) VALUES (%s, %s, %s)",
                (list_id, f"SMOKE-LST-{list_id.hex[:8]}", "Smoke Service List"),
            )
            _insert_customer(conn, cust_id, code, phone, price_list_id=list_id)
            _insert_product(conn, p1)
            _insert_inventory(conn, p1, 10)
            _insert_price(conn, list_id, p1, Decimal("99.50"))
            _insert_price(conn, general.id, p1, Decimal("55.00"))  # must be ignored
        inserted = True

        detail = _service().create_draft_order(
            OrderCreate(
                customer_id=cust_id,
                items=[OrderItemCreate(product_id=p1, quantity=2)],
            )
        )

        assert detail.status is OrderStatus.DRAFT
        (item,) = detail.items
        assert item.unit_price == Decimal("99.50")  # customer's list, not GENERAL
        assert item.subtotal == Decimal("199.00")
        assert detail.total_amount == Decimal("199.00")

        with transaction() as conn:
            _assert_balance_unchanged(conn, cust_id)
            _assert_inventory_unchanged(conn, p1, 10)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1], list_id)


def test_create_draft_order_missing_price_raises_and_writes_nothing():
    cust_id = uuid4()
    p1 = uuid4()
    code = f"SMOKESVC-{cust_id.hex[:8]}"
    phone = f"+62895{cust_id.int % 1_000_000_000:09d}"

    inserted = False
    try:
        with transaction() as conn:
            _insert_customer(conn, cust_id, code, phone)  # GENERAL fallback
            _insert_product(conn, p1)
            _insert_inventory(conn, p1, 7)
            # no product_prices row: price resolution must fail
        inserted = True

        with pytest.raises(ProductPriceNotFoundError) as excinfo:
            _service().create_draft_order(
                OrderCreate(
                    customer_id=cust_id,
                    items=[OrderItemCreate(product_id=p1, quantity=1)],
                )
            )
        assert excinfo.value.product_id == p1

        with transaction() as conn:
            assert _order_count(conn, cust_id) == 0
            assert _item_count(conn, [p1]) == 0
            _assert_balance_unchanged(conn, cust_id)
            _assert_inventory_unchanged(conn, p1, 7)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1], None)


def test_create_draft_order_rolls_back_when_total_update_fails(monkeypatch):
    cust_id = uuid4()
    p1, p2 = uuid4(), uuid4()
    code = f"SMOKESVC-{cust_id.hex[:8]}"
    phone = f"+62894{cust_id.int % 1_000_000_000:09d}"

    inserted = False
    try:
        with transaction() as conn:
            general = PriceRepository().get_price_list_by_code(
                conn, GENERAL_PRICE_LIST_CODE
            )
            assert general is not None
            _insert_customer(conn, cust_id, code, phone)
            _insert_product(conn, p1)
            _insert_product(conn, p2)
            _insert_inventory(conn, p1, 100)
            _insert_inventory(conn, p2, 50)
            _insert_price(conn, general.id, p1, Decimal("150.00"))
            _insert_price(conn, general.id, p2, Decimal("2500.00"))
        inserted = True

        # fail after order header + items are written: the whole
        # workflow must roll back, leaving nothing behind
        def _boom(self, conn, order_id, total_amount):
            raise psycopg.OperationalError("forced total-update failure")

        monkeypatch.setattr(OrderRepository, "update_total_amount", _boom)
        with pytest.raises(psycopg.OperationalError):
            _service().create_draft_order(
                OrderCreate(
                    customer_id=cust_id,
                    items=[
                        OrderItemCreate(product_id=p1, quantity=2),
                        OrderItemCreate(product_id=p2, quantity=1),
                    ],
                )
            )

        with transaction() as conn:
            assert _order_count(conn, cust_id) == 0  # header rolled back
            assert _item_count(conn, [p1, p2]) == 0  # items rolled back
            _assert_balance_unchanged(conn, cust_id)
            _assert_inventory_unchanged(conn, p1, 100)
            _assert_inventory_unchanged(conn, p2, 50)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


# ---------------------------------------------------------------------------
# confirm_order
# ---------------------------------------------------------------------------


def test_confirm_order_success_reserves_stock_and_transitions():
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    order_id = None
    try:
        with transaction() as conn:
            _seed_confirm_world(conn, cust_id, p1, p2, credit_limit=Decimal("10000.00"))
        inserted = True

        order_id = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id

        detail = _service().confirm_order(order_id)

        assert isinstance(detail, OrderDetailResponse)
        assert detail.id == order_id
        assert detail.status is OrderStatus.CONFIRMED
        assert detail.confirmed_at is not None
        assert detail.total_amount == Decimal("2800.00")
        assert detail.delivered_at is None and detail.cancelled_at is None

        with transaction() as conn:  # fresh read after commit
            persisted = OrderRepository().get_by_id(conn, order_id)
            assert persisted is not None
            assert persisted.status is OrderStatus.CONFIRMED
            assert persisted.confirmed_at is not None
            _assert_inventory_reserved(conn, p1, 100, 2)
            _assert_inventory_reserved(conn, p2, 50, 1)
            _assert_balance_unchanged(conn, cust_id)  # confirm moves no balance
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_confirm_order_insufficient_stock_rejects_entirely():
    # p1 has plenty of stock, p2 none: the whole order must stay draft
    # with zero reservation (all-or-nothing)
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    order_id = None
    try:
        with transaction() as conn:
            _seed_confirm_world(
                conn, cust_id, p1, p2, credit_limit=Decimal("10000.00"), p2_stock=0
            )
        inserted = True

        order_id = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id

        with pytest.raises(InsufficientStockError) as excinfo:
            _service().confirm_order(order_id)
        assert excinfo.value.product_id == p2
        assert excinfo.value.requested == 1
        assert excinfo.value.available == 0

        with transaction() as conn:
            persisted = OrderRepository().get_by_id(conn, order_id)
            assert persisted is not None
            assert persisted.status is OrderStatus.DRAFT  # stays draft
            assert persisted.confirmed_at is None
            _assert_inventory_reserved(conn, p1, 100, 0)  # nothing reserved
            _assert_inventory_reserved(conn, p2, 0, 0)
            _assert_balance_unchanged(conn, cust_id)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_confirm_order_credit_exposure_boundaries():
    # standing confirmed exposure 3000.00 + draft 2800.00 = 5800.00:
    # exactly at a 5800.00 limit passes, then a second 2800.00 draft
    # pushes projected to 8600.00 > 5800.00 and is rejected
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    try:
        with transaction() as conn:
            _seed_confirm_world(conn, cust_id, p1, p2, credit_limit=Decimal("5800.00"))
            conn.execute(
                "INSERT INTO orders (customer_id, status, total_amount, confirmed_at) "
                "VALUES (%s, 'confirmed', %s, now())",
                (cust_id, Decimal("3000.00")),
            )
        inserted = True

        first = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id
        detail = _service().confirm_order(first)  # 3000.00 + 2800.00 == 5800.00
        assert detail.status is OrderStatus.CONFIRMED

        second = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id
        with pytest.raises(CreditLimitExceededError) as excinfo:
            _service().confirm_order(second)
        assert excinfo.value.credit_limit == Decimal("5800.00")
        assert excinfo.value.projected_exposure == Decimal("8600.00")

        with transaction() as conn:
            persisted = OrderRepository().get_by_id(conn, second)
            assert persisted is not None
            assert persisted.status is OrderStatus.DRAFT  # rejected one stays draft
            # only the first order's items are reserved
            _assert_inventory_reserved(conn, p1, 100, 2)
            _assert_inventory_reserved(conn, p2, 50, 1)
            _assert_balance_unchanged(conn, cust_id)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_confirm_order_zero_credit_limit_blocks():
    # 0 is a CONFIGURED limit (migration 002): positive order rejected
    # via CreditLimitExceededError, never treated as unconfigured
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    order_id = None
    try:
        with transaction() as conn:
            _seed_confirm_world(conn, cust_id, p1, p2, credit_limit=Decimal("0.00"))
        inserted = True

        order_id = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id

        with pytest.raises(CreditLimitExceededError) as excinfo:
            _service().confirm_order(order_id)
        assert excinfo.value.credit_limit == Decimal("0.00")
        assert excinfo.value.projected_exposure == Decimal("2800.00")

        with transaction() as conn:
            persisted = OrderRepository().get_by_id(conn, order_id)
            assert persisted is not None
            assert persisted.status is OrderStatus.DRAFT
            _assert_inventory_reserved(conn, p1, 100, 0)
            _assert_inventory_reserved(conn, p2, 50, 0)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_confirm_order_null_credit_limit_blocks():
    # NULL = not configured (migration 002): draft creation is fine,
    # confirmation is rejected, nothing is reserved
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    order_id = None
    try:
        with transaction() as conn:
            _seed_confirm_world(conn, cust_id, p1, p2, credit_limit=None)
        inserted = True

        order_id = (
            _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id
        )  # drafts never check credit

        with pytest.raises(CreditLimitNotConfiguredError) as excinfo:
            _service().confirm_order(order_id)
        assert excinfo.value.customer_id == cust_id

        with transaction() as conn:
            persisted = OrderRepository().get_by_id(conn, order_id)
            assert persisted is not None
            assert persisted.status is OrderStatus.DRAFT  # stays draft
            assert persisted.confirmed_at is None
            _assert_inventory_reserved(conn, p1, 100, 0)  # nothing reserved
            _assert_inventory_reserved(conn, p2, 50, 0)
            _assert_balance_unchanged(conn, cust_id)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_credit_limit_null_and_zero_are_distinct_after_migration():
    # migration 002: omitted credit_limit stores NULL (no implicit
    # zero default); an explicit zero stays exactly zero
    cust_null, cust_zero = uuid4(), uuid4()

    inserted = False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO customers (id, code, name, phone) "  # column omitted
                "VALUES (%s, %s, %s, %s)",
                (
                    cust_null,
                    f"SMOKE-NULL-{cust_null.hex[:8]}",
                    "Null Limit",
                    f"+62892{cust_null.int % 1_000_000_000:09d}",
                ),
            )
            _insert_customer(
                conn,
                cust_zero,
                f"SMOKE-ZERO-{cust_zero.hex[:8]}",
                f"+62891{cust_zero.int % 1_000_000_000:09d}",
                credit_limit=Decimal("0.00"),
            )
        inserted = True

        with transaction() as conn:
            unconfigured = CustomerRepository().get_by_id(conn, cust_null)
            assert unconfigured is not None
            assert unconfigured.credit_limit is None  # NULL, not zero

            zero = CustomerRepository().get_by_id(conn, cust_zero)
            assert zero is not None
            assert zero.credit_limit == Decimal("0.00")  # zero, not NULL
    finally:
        if inserted:
            with transaction() as conn:
                for cust in (cust_null, cust_zero):
                    conn.execute("DELETE FROM customers WHERE id = %s", (cust,))


def test_confirm_order_inactive_customer_still_confirmable():
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    order_id = None
    try:
        with transaction() as conn:
            _seed_confirm_world(conn, cust_id, p1, p2, credit_limit=Decimal("10000.00"))
        inserted = True

        order_id = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id

        with transaction() as conn:  # customer deactivated after the draft
            conn.execute(
                "UPDATE customers SET is_active = false WHERE id = %s", (cust_id,)
            )

        detail = _service().confirm_order(order_id)
        assert detail.status is OrderStatus.CONFIRMED

        with transaction() as conn:
            _assert_inventory_reserved(conn, p1, 100, 2)
            _assert_inventory_reserved(conn, p2, 50, 1)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_confirm_order_inactive_product_still_confirmable():
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    order_id = None
    try:
        with transaction() as conn:
            _seed_confirm_world(conn, cust_id, p1, p2, credit_limit=Decimal("10000.00"))
        inserted = True

        order_id = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id

        with transaction() as conn:  # products deactivated after the draft
            conn.execute(
                "UPDATE products SET is_active = false WHERE id = ANY(%s)",
                ([p1, p2],),
            )

        detail = _service().confirm_order(order_id)
        assert detail.status is OrderStatus.CONFIRMED

        with transaction() as conn:
            _assert_inventory_reserved(conn, p1, 100, 2)
            _assert_inventory_reserved(conn, p2, 50, 1)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_confirm_order_missing_inventory_row():
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    order_id = None
    try:
        with transaction() as conn:
            _seed_confirm_world(conn, cust_id, p1, p2, credit_limit=Decimal("10000.00"))
            conn.execute("DELETE FROM inventory WHERE product_id = %s", (p2,))
        inserted = True

        order_id = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id

        with pytest.raises(InventoryNotFoundError) as excinfo:
            _service().confirm_order(order_id)
        assert excinfo.value.product_id == p2

        with transaction() as conn:
            persisted = OrderRepository().get_by_id(conn, order_id)
            assert persisted is not None
            assert persisted.status is OrderStatus.DRAFT
            _assert_inventory_reserved(conn, p1, 100, 0)  # nothing reserved
            _assert_balance_unchanged(conn, cust_id)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_confirm_order_twice_rejects_second():
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    order_id = None
    try:
        with transaction() as conn:
            _seed_confirm_world(conn, cust_id, p1, p2, credit_limit=Decimal("10000.00"))
        inserted = True

        order_id = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id

        assert _service().confirm_order(order_id).status is OrderStatus.CONFIRMED
        with pytest.raises(InvalidOrderStatusError) as excinfo:
            _service().confirm_order(order_id)
        assert excinfo.value.current_status is OrderStatus.CONFIRMED
        assert excinfo.value.target_status == "confirmed"

        with transaction() as conn:
            # no double reservation: still exactly the first order's items
            _assert_inventory_reserved(conn, p1, 100, 2)
            _assert_inventory_reserved(conn, p2, 50, 1)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def test_confirm_order_concurrent_same_customer_serializes_on_credit():
    # two drafts of 2800.00 each against a 5000.00 limit: combined 5600.00
    # exceeds it, so exactly ONE confirmation may succeed — the customer
    # row lock serializes the two credit checks
    cust_id, p1, p2 = uuid4(), uuid4(), uuid4()

    inserted = False
    try:
        with transaction() as conn:
            _seed_confirm_world(conn, cust_id, p1, p2, credit_limit=Decimal("5000.00"))
        inserted = True

        first = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id
        second = _service().create_draft_order(_two_item_draft(cust_id, p1, p2)).id

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda oid: _collect(_service().confirm_order, oid),
                    [first, second],
                )
            )

        outcomes = [type(r) for r in results]
        successes = [r for r in results if isinstance(r, OrderDetailResponse)]
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(successes) == 1, f"exactly one confirmation: {outcomes!r}"
        assert len(failures) == 1
        assert isinstance(failures[0], CreditLimitExceededError)
        assert successes[0].status is OrderStatus.CONFIRMED

        with transaction() as conn:
            # one order confirmed, one still draft; single reservation set
            states = {}
            for oid in (first, second):
                row = conn.execute(
                    "SELECT status FROM orders WHERE id = %s", (oid,)
                ).fetchone()
                states[oid] = row["status"]
            assert sorted(states.values()) == ["confirmed", "draft"]
            _assert_inventory_reserved(conn, p1, 100, 2)
            _assert_inventory_reserved(conn, p2, 50, 1)
            _assert_balance_unchanged(conn, cust_id)
    finally:
        if inserted:
            with transaction() as conn:
                _cleanup(conn, cust_id, [p1, p2], None)


def _collect(fn, *args):
    """Run fn, returning its result or its exception (never raising)."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 — test collects outcomes only
        return exc
