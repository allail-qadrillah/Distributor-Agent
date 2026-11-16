"""OrderService unit tests — mocked repositories and transaction, no live database.

Under test: the create_draft_order workflow (validation order, price
list resolution (customer list vs GENERAL fallback), backend-resolved
unit prices, Decimal totals built from database subtotals) and the
confirm_order workflow (credit exposure, all-or-nothing reservation,
guarded draft -> confirmed transition, lock discipline). Shared for
both: one connection per repository call, commit/rollback semantics.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest

import app.services.order as order_service_module
from app.schemas.customer import CustomerResponse
from app.schemas.inventory import InventoryResponse
from app.schemas.order import (
    OrderCreate,
    OrderDetailResponse,
    OrderItemCreate,
    OrderItemInsert,
    OrderItemResponse,
    OrderResponse,
    OrderStatus,
)
from app.schemas.price_list import PriceListResponse, ProductPriceResponse
from app.schemas.product import ProductResponse
from app.services.exceptions import (
    CreditLimitExceededError,
    CreditLimitNotConfiguredError,
    CustomerNotFoundError,
    EmptyOrderError,
    InactiveCustomerError,
    InactiveProductError,
    InsufficientStockError,
    InvalidOrderStatusError,
    InventoryNotFoundError,
    OrderNotFoundError,
    OrderTotalUpdateError,
    PriceListNotFoundError,
    ProductNotFoundError,
    ProductPriceNotFoundError,
)
from app.services.order import GENERAL_PRICE_LIST_CODE, OrderService

NOW = datetime.now(UTC)
CUSTOMER_ID = uuid4()
PRODUCT_ID = uuid4()
OTHER_PRODUCT_ID = uuid4()
CUSTOMER_LIST_ID = uuid4()
GENERAL_LIST_ID = uuid4()
ORDER_ID = uuid4()

PRICE_P1 = Decimal("150.00")
PRICE_P2 = Decimal("2500.00")
SUBTOTAL_P1 = Decimal("300.00")
SUBTOTAL_P2 = Decimal("2500.00")
TOTAL = Decimal("2800.00")


# ---------------------------------------------------------------------------
# Fixtures: schema objects, fake repositories, fake transaction
# ---------------------------------------------------------------------------


def _customer(
    *,
    price_list_id: UUID | None = CUSTOMER_LIST_ID,
    is_active: bool = True,
    credit_limit: Decimal | None = Decimal("10000.00"),
    outstanding_balance: Decimal = Decimal("500.00"),
) -> CustomerResponse:
    return CustomerResponse(
        id=CUSTOMER_ID,
        code="CUST-001",
        name="Toko Sumber Makmur",
        phone="+628123456789",
        address="Jl. Merdeka 1",
        credit_limit=credit_limit,
        outstanding_balance=outstanding_balance,
        price_list_id=price_list_id,
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def _product(product_id: UUID, *, is_active: bool = True) -> ProductResponse:
    return ProductResponse(
        id=product_id,
        sku=f"SKU-{product_id.hex[:8]}",
        name=f"Product {product_id.hex[:4]}",
        unit="pcs",
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


def _price_list(price_list_id: UUID) -> PriceListResponse:
    return PriceListResponse(
        id=price_list_id,
        code=f"PL-{price_list_id.hex[:8]}",
        name=f"List {price_list_id.hex[:4]}",
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _product_price(
    price_list_id: UUID, product_id: UUID, unit_price: Decimal
) -> ProductPriceResponse:
    return ProductPriceResponse(
        id=uuid4(),
        price_list_id=price_list_id,
        product_id=product_id,
        unit_price=unit_price,
        created_at=NOW,
        updated_at=NOW,
    )


def _order(
    total: Decimal = Decimal("0.00"),
    *,
    status: OrderStatus = OrderStatus.DRAFT,
    confirmed_at: datetime | None = None,
) -> OrderResponse:
    return OrderResponse(
        id=ORDER_ID,
        order_number="ORD-00000042",
        customer_id=CUSTOMER_ID,
        status=status,
        total_amount=total,
        notes=None,
        confirmed_at=confirmed_at,
        cancelled_at=None,
        delivered_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _stored_item(
    product_id: UUID, quantity: int, unit_price: Decimal, subtotal: Decimal
) -> OrderItemResponse:
    return OrderItemResponse(
        id=uuid4(),
        order_id=ORDER_ID,
        product_id=product_id,
        quantity=quantity,
        unit_price=unit_price,
        subtotal=subtotal,
        created_at=NOW,
    )


def _detail(
    items: list[OrderItemResponse],
    total: Decimal,
    *,
    status: OrderStatus = OrderStatus.DRAFT,
    confirmed_at: datetime | None = None,
) -> OrderDetailResponse:
    return OrderDetailResponse(
        id=ORDER_ID,
        order_number="ORD-00000042",
        customer_id=CUSTOMER_ID,
        status=status,
        total_amount=total,
        notes="deliver before noon",
        confirmed_at=confirmed_at,
        cancelled_at=None,
        delivered_at=None,
        created_at=NOW,
        updated_at=NOW,
        items=items,
    )


def _inventory(product_id: UUID, on_hand: int, reserved: int = 0) -> InventoryResponse:
    return InventoryResponse(
        id=uuid4(),
        product_id=product_id,
        on_hand_stock=on_hand,
        reserved_stock=reserved,
        available_stock=on_hand - reserved,  # as the generated column returns it
        updated_at=NOW,
    )


def _stored_items() -> list[OrderItemResponse]:
    return [
        _stored_item(PRODUCT_ID, 2, PRICE_P1, SUBTOTAL_P1),
        _stored_item(OTHER_PRODUCT_ID, 1, PRICE_P2, SUBTOTAL_P2),
    ]


ORDER_CREATE = OrderCreate(
    customer_id=CUSTOMER_ID,
    notes="deliver before noon",
    items=[
        OrderItemCreate(product_id=PRODUCT_ID, quantity=2),
        OrderItemCreate(product_id=OTHER_PRODUCT_ID, quantity=1),
    ],
)


class FakeConn:
    """Connection identity token — recorded by every fake repository."""


class FakeTransaction:
    """Stands in for app.database.connection.transaction().

    Yields one shared FakeConn and records commit/rollback exactly like
    the real context manager (exceptions re-raised, never swallowed).
    """

    def __init__(self) -> None:
        self.conn = FakeConn()
        self.journal: list[str] = []

    def __call__(self) -> "FakeTransaction":
        return self

    def __enter__(self) -> FakeConn:
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.journal.append("commit" if exc_type is None else "rollback")
        return False


class FakeCustomerRepository:
    def __init__(
        self,
        customer: CustomerResponse | None,
        locked_customer: CustomerResponse | None = None,
    ) -> None:
        self.customer = customer
        self.locked_customer = customer if locked_customer is None else locked_customer
        self.calls: list[tuple] = []

    def get_by_id(self, conn, customer_id):
        self.calls.append(("get_by_id", conn, customer_id))
        return self.customer

    def get_for_update(self, conn, customer_id):
        self.calls.append(("get_for_update", conn, customer_id))
        return self.locked_customer


class FakeProductRepository:
    def __init__(self, products: dict[UUID, ProductResponse | None]) -> None:
        self.products = products
        self.calls: list[tuple] = []

    def get_by_id(self, conn, product_id):
        self.calls.append(("get_by_id", conn, product_id))
        return self.products.get(product_id)


class FakePriceRepository:
    def __init__(
        self,
        price_lists_by_id: dict[UUID, PriceListResponse | None] | None = None,
        price_lists_by_code: dict[str, PriceListResponse | None] | None = None,
        product_prices: dict[tuple[UUID, UUID], ProductPriceResponse | None]
        | None = None,
    ) -> None:
        self.by_id = price_lists_by_id or {}
        self.by_code = price_lists_by_code or {}
        self.prices = product_prices or {}
        self.calls: list[tuple] = []

    def get_price_list_by_id(self, conn, price_list_id):
        self.calls.append(("get_price_list_by_id", conn, price_list_id))
        return self.by_id.get(price_list_id)

    def get_price_list_by_code(self, conn, code):
        self.calls.append(("get_price_list_by_code", conn, code))
        return self.by_code.get(code)

    def get_product_price(self, conn, price_list_id, product_id):
        self.calls.append(("get_product_price", conn, price_list_id, product_id))
        return self.prices.get((price_list_id, product_id))


class FakeOrderRepository:
    def __init__(
        self,
        *,
        order: OrderResponse | None = None,
        items: list[OrderItemResponse] | None = None,
        updated: OrderResponse | None = None,
        detail: OrderDetailResponse | None = None,
        item_error: Exception | None = None,
        update_error: Exception | None = None,
        locked_detail: OrderDetailResponse | None = None,
        confirmed_total: Decimal = Decimal("0.00"),
        marked: OrderResponse | None = None,
        mark_error: Exception | None = None,
    ) -> None:
        self.order = order
        self.items = items if items is not None else []
        self.updated = updated
        self.detail = detail
        self.item_error = item_error
        self.update_error = update_error
        self.locked_detail = locked_detail
        self.confirmed_total = confirmed_total
        self.marked = marked
        self.mark_error = mark_error
        self.calls: list[tuple] = []

    def create_order(self, conn, order):
        self.calls.append(("create_order", conn, order))
        return self.order

    def create_order_items(self, conn, order_id, items):
        self.calls.append(("create_order_items", conn, order_id, items))
        if self.item_error is not None:
            raise self.item_error
        return self.items

    def update_total_amount(self, conn, order_id, total_amount):
        self.calls.append(("update_total_amount", conn, order_id, total_amount))
        if self.update_error is not None:
            raise self.update_error
        return self.updated

    def get_by_id(self, conn, order_id):
        self.calls.append(("get_by_id", conn, order_id))
        return self.detail

    def get_for_update(self, conn, order_id):
        self.calls.append(("get_for_update", conn, order_id))
        return self.locked_detail

    def sum_confirmed_totals(self, conn, customer_id):
        self.calls.append(("sum_confirmed_totals", conn, customer_id))
        return self.confirmed_total

    def mark_confirmed(self, conn, order_id):
        self.calls.append(("mark_confirmed", conn, order_id))
        if self.mark_error is not None:
            raise self.mark_error
        return self.marked


class FakeInventoryRepository:
    """Records lock/reserve calls; tracks reserved quantities per product."""

    def __init__(
        self,
        inventory: dict[UUID, InventoryResponse] | None = None,
        *,
        reserve_error: Exception | None = None,
    ) -> None:
        self.inventory = inventory or {}
        self.reserve_error = reserve_error
        self.calls: list[tuple] = []
        self.reserved: dict[UUID, int] = {}

    def lock_by_product_ids(self, conn, product_ids):
        self.calls.append(("lock_by_product_ids", conn, list(product_ids)))
        return [
            self.inventory[pid] for pid in sorted(product_ids) if pid in self.inventory
        ]

    def reserve_stock(self, conn, product_id, quantity):
        self.calls.append(("reserve_stock", conn, product_id, quantity))
        if self.reserve_error is not None:
            raise self.reserve_error
        self.reserved[product_id] = self.reserved.get(product_id, 0) + quantity
        return self.inventory.get(product_id)


@pytest.fixture
def tx(monkeypatch) -> FakeTransaction:
    """Replace app.services.order.transaction with the recording fake."""
    fake = FakeTransaction()
    monkeypatch.setattr(order_service_module, "transaction", fake)
    return fake


def _world(**overrides):
    """Happy-path world: active customer with own list, two priced products.

    Returns (customers, products, prices, orders, inventory) fakes in
    OrderService constructor order; any part can be overridden per test.
    Defaults make both workflows succeed: draft-creation writes the two
    items, and confirm passes the credit check (balance 500.00 +
    confirmed totals 1000.00 + order total 2800.00 = 4300.00 exposure
    against a 10000.00 limit) with stock to spare.
    """
    customer = overrides.pop("customer", _customer())
    products = overrides.pop(
        "products",
        {
            PRODUCT_ID: _product(PRODUCT_ID),
            OTHER_PRODUCT_ID: _product(OTHER_PRODUCT_ID),
        },
    )
    prices = overrides.pop(
        "prices",
        FakePriceRepository(
            price_lists_by_id={CUSTOMER_LIST_ID: _price_list(CUSTOMER_LIST_ID)},
            product_prices={
                (CUSTOMER_LIST_ID, PRODUCT_ID): _product_price(
                    CUSTOMER_LIST_ID, PRODUCT_ID, PRICE_P1
                ),
                (CUSTOMER_LIST_ID, OTHER_PRODUCT_ID): _product_price(
                    CUSTOMER_LIST_ID, OTHER_PRODUCT_ID, PRICE_P2
                ),
            },
        ),
    )
    stored_items = _stored_items()
    orders = overrides.pop(
        "orders",
        FakeOrderRepository(
            order=_order(),
            items=stored_items,
            updated=_order(TOTAL),
            detail=_detail(stored_items, TOTAL),
            locked_detail=_detail(stored_items, TOTAL),
            confirmed_total=Decimal("1000.00"),
            marked=_order(TOTAL, status=OrderStatus.CONFIRMED, confirmed_at=NOW),
        ),
    )
    inventory = overrides.pop(
        "inventory",
        FakeInventoryRepository(
            {
                PRODUCT_ID: _inventory(PRODUCT_ID, 100),
                OTHER_PRODUCT_ID: _inventory(OTHER_PRODUCT_ID, 50),
            }
        ),
    )
    assert not overrides, f"unknown overrides: {overrides}"
    return (
        FakeCustomerRepository(customer),
        FakeProductRepository(products),
        prices,
        orders,
        inventory,
    )


def _names(repo) -> list[str]:
    return [call[0] for call in repo.calls]


# ---------------------------------------------------------------------------
# 1-2. success: workflow order, resolved items, commit
# ---------------------------------------------------------------------------


def test_create_draft_order_success(tx):
    customers, products, prices, orders, inventory = _world()
    result = OrderService(
        customers, products, prices, orders, inventory
    ).create_draft_order(ORDER_CREATE)

    assert isinstance(result, OrderDetailResponse)
    assert tx.journal == ["commit"]


def test_create_draft_order_returns_detail_response(tx):
    customers, products, prices, orders, inventory = _world()
    result = OrderService(
        customers, products, prices, orders, inventory
    ).create_draft_order(ORDER_CREATE)

    assert result is orders.detail
    assert result is not None
    detail = result
    assert detail.id == ORDER_ID
    assert detail.customer_id == CUSTOMER_ID
    assert detail.order_number == "ORD-00000042"
    assert detail.status is OrderStatus.DRAFT
    assert detail.total_amount == TOTAL
    assert detail.created_at == NOW and detail.updated_at == NOW
    assert [(i.unit_price, i.subtotal) for i in detail.items] == [
        (PRICE_P1, SUBTOTAL_P1),
        (PRICE_P2, SUBTOTAL_P2),
    ]


def test_create_draft_order_workflow_call_order(tx):
    customers, products, prices, orders, inventory = _world()
    OrderService(customers, products, prices, orders, inventory).create_draft_order(
        ORDER_CREATE
    )

    assert _names(orders) == [
        "create_order",
        "create_order_items",
        "update_total_amount",
        "get_by_id",
    ]


def test_create_draft_order_resolves_backend_prices(tx):
    customers, products, prices, orders, inventory = _world()
    OrderService(customers, products, prices, orders, inventory).create_draft_order(
        ORDER_CREATE
    )

    items_call = orders.calls[1]
    assert items_call[0] == "create_order_items"
    assert items_call[1] is tx.conn
    assert items_call[2] == ORDER_ID
    sent = items_call[3]
    assert all(isinstance(i, OrderItemInsert) for i in sent)
    # unit prices come from the price repository, never from the client
    assert [(i.product_id, i.quantity, i.unit_price) for i in sent] == [
        (PRODUCT_ID, 2, PRICE_P1),
        (OTHER_PRODUCT_ID, 1, PRICE_P2),
    ]


def test_create_draft_order_keeps_item_order_for_multiple_items(tx):
    customers, products, prices, orders, inventory = _world()
    OrderService(customers, products, prices, orders, inventory).create_draft_order(
        ORDER_CREATE
    )

    sent = orders.calls[1][3]
    assert [i.product_id for i in sent] == [PRODUCT_ID, OTHER_PRODUCT_ID]
    assert [i.quantity for i in sent] == [2, 1]


# ---------------------------------------------------------------------------
# 3-5. customer validation
# ---------------------------------------------------------------------------


def test_customer_not_found_rolls_back(tx):
    customers, products, prices, orders, inventory = _world(customer=None)
    with pytest.raises(CustomerNotFoundError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert excinfo.value.customer_id == CUSTOMER_ID
    assert tx.journal == ["rollback"]
    assert orders.calls == []  # nothing written


def test_inactive_customer_rejected(tx):
    customers, products, prices, orders, inventory = _world(
        customer=_customer(is_active=False)
    )
    with pytest.raises(InactiveCustomerError):
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert tx.journal == ["rollback"]
    assert orders.calls == []


# ---------------------------------------------------------------------------
# 6-9. price list resolution
# ---------------------------------------------------------------------------


def test_customer_specific_price_list_used(tx):
    customers, products, prices, orders, inventory = _world()
    OrderService(customers, products, prices, orders, inventory).create_draft_order(
        ORDER_CREATE
    )

    assert ("get_price_list_by_id", tx.conn, CUSTOMER_LIST_ID) in prices.calls
    assert "get_price_list_by_code" not in _names(prices)  # no GENERAL fallback
    # product prices resolved against the customer's list
    assert ("get_product_price", tx.conn, CUSTOMER_LIST_ID, PRODUCT_ID) in prices.calls
    assert (
        "get_product_price",
        tx.conn,
        CUSTOMER_LIST_ID,
        OTHER_PRODUCT_ID,
    ) in prices.calls


def test_general_price_list_fallback(tx):
    prices = FakePriceRepository(
        price_lists_by_code={GENERAL_PRICE_LIST_CODE: _price_list(GENERAL_LIST_ID)},
        product_prices={
            (GENERAL_LIST_ID, PRODUCT_ID): _product_price(
                GENERAL_LIST_ID, PRODUCT_ID, PRICE_P1
            ),
            (GENERAL_LIST_ID, OTHER_PRODUCT_ID): _product_price(
                GENERAL_LIST_ID, OTHER_PRODUCT_ID, PRICE_P2
            ),
        },
    )
    customers, products, _, orders, inventory = _world(
        customer=_customer(price_list_id=None), prices=prices
    )
    result = OrderService(
        customers, products, prices, orders, inventory
    ).create_draft_order(ORDER_CREATE)

    assert result is not None
    assert ("get_price_list_by_code", tx.conn, GENERAL_PRICE_LIST_CODE) in prices.calls
    assert "get_price_list_by_id" not in _names(prices)
    assert ("get_product_price", tx.conn, GENERAL_LIST_ID, PRODUCT_ID) in prices.calls
    sent = orders.calls[1][3]
    assert [i.unit_price for i in sent] == [PRICE_P1, PRICE_P2]


def test_customer_price_list_missing(tx):
    customers, products, prices, orders, inventory = _world(
        prices=FakePriceRepository(price_lists_by_id={})
    )
    with pytest.raises(PriceListNotFoundError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert excinfo.value.price_list == CUSTOMER_LIST_ID
    assert tx.journal == ["rollback"]
    assert orders.calls == []


def test_general_price_list_missing(tx):
    prices = FakePriceRepository()  # nothing resolvable
    customers, products, _, orders, inventory = _world(
        customer=_customer(price_list_id=None), prices=prices
    )
    with pytest.raises(PriceListNotFoundError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert excinfo.value.price_list == GENERAL_PRICE_LIST_CODE
    assert tx.journal == ["rollback"]
    assert orders.calls == []


# ---------------------------------------------------------------------------
# 10-11. product validation and price resolution
# ---------------------------------------------------------------------------


def test_product_not_found(tx):
    customers, products, prices, orders, inventory = _world(
        products={PRODUCT_ID: _product(PRODUCT_ID)}
    )
    with pytest.raises(ProductNotFoundError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert excinfo.value.product_id == OTHER_PRODUCT_ID
    assert tx.journal == ["rollback"]
    assert orders.calls == []


def test_inactive_product_rejected(tx):
    customers, products, prices, orders, inventory = _world(
        products={
            PRODUCT_ID: _product(PRODUCT_ID),
            OTHER_PRODUCT_ID: _product(OTHER_PRODUCT_ID, is_active=False),
        }
    )
    with pytest.raises(InactiveProductError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert excinfo.value.product_id == OTHER_PRODUCT_ID
    assert tx.journal == ["rollback"]
    assert orders.calls == []


def test_missing_product_price(tx):
    customers, products, prices, orders, inventory = _world(
        prices=FakePriceRepository(
            price_lists_by_id={CUSTOMER_LIST_ID: _price_list(CUSTOMER_LIST_ID)},
            product_prices={
                (CUSTOMER_LIST_ID, PRODUCT_ID): _product_price(
                    CUSTOMER_LIST_ID, PRODUCT_ID, PRICE_P1
                )
            },
        )
    )
    with pytest.raises(ProductPriceNotFoundError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert excinfo.value.product_id == OTHER_PRODUCT_ID
    assert excinfo.value.price_list_id == CUSTOMER_LIST_ID
    assert tx.journal == ["rollback"]
    assert orders.calls == []


# ---------------------------------------------------------------------------
# 12-13. total calculation: Decimal, database-generated subtotals
# ---------------------------------------------------------------------------


def test_total_is_decimal_sum_of_db_subtotals(tx):
    customers, products, prices, orders, inventory = _world()
    OrderService(customers, products, prices, orders, inventory).create_draft_order(
        ORDER_CREATE
    )

    update_call = orders.calls[2]
    assert update_call[0] == "update_total_amount"
    assert update_call[1] is tx.conn and update_call[2] == ORDER_ID
    total = update_call[3]
    assert isinstance(total, Decimal)
    assert total == TOTAL  # 300.00 + 2500.00, summed from DB subtotals


def test_total_uses_decimal_not_float(tx):
    # 0.10 + 0.20: float arithmetic would produce 0.30000000000000004;
    # Decimal must produce exactly 0.30.
    prices = FakePriceRepository(
        price_lists_by_id={CUSTOMER_LIST_ID: _price_list(CUSTOMER_LIST_ID)},
        product_prices={
            (CUSTOMER_LIST_ID, PRODUCT_ID): _product_price(
                CUSTOMER_LIST_ID, PRODUCT_ID, Decimal("0.10")
            ),
            (CUSTOMER_LIST_ID, OTHER_PRODUCT_ID): _product_price(
                CUSTOMER_LIST_ID, OTHER_PRODUCT_ID, Decimal("0.20")
            ),
        },
    )
    stored = [
        _stored_item(PRODUCT_ID, 1, Decimal("0.10"), Decimal("0.10")),
        _stored_item(OTHER_PRODUCT_ID, 1, Decimal("0.20"), Decimal("0.20")),
    ]
    orders = FakeOrderRepository(
        order=_order(),
        items=stored,
        updated=_order(Decimal("0.30")),
        detail=_detail(stored, Decimal("0.30")),
    )
    customers, products, _, _orders, inventory = _world(prices=prices, orders=orders)
    result = OrderService(
        customers, products, prices, orders, inventory
    ).create_draft_order(ORDER_CREATE)

    total = orders.calls[2][3]
    assert isinstance(total, Decimal)
    assert total == Decimal("0.30")
    assert result is not None and result.total_amount == Decimal("0.30")


# ---------------------------------------------------------------------------
# 14-16. write failures: rollback, propagation, no swallow
# ---------------------------------------------------------------------------


def test_failure_during_item_insertion_rolls_back(tx):
    customers, products, prices, orders, inventory = _world(
        orders=FakeOrderRepository(
            order=_order(), item_error=psycopg.OperationalError()
        )
    )
    with pytest.raises(psycopg.OperationalError):
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert tx.journal == ["rollback"]
    # total update and read-back never happen after the failure
    assert _names(orders) == ["create_order", "create_order_items"]


def test_failure_during_total_update_rolls_back(tx):
    customers, products, prices, orders, inventory = _world(
        orders=FakeOrderRepository(
            order=_order(), items=[], update_error=psycopg.OperationalError()
        )
    )
    with pytest.raises(psycopg.OperationalError):
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert tx.journal == ["rollback"]
    assert _names(orders) == [
        "create_order",
        "create_order_items",
        "update_total_amount",
    ]
    assert "get_by_id" not in _names(orders)


def test_total_update_returning_no_order_raises_domain_error(tx):
    customers, products, prices, orders, inventory = _world(
        orders=FakeOrderRepository(order=_order(), items=[], updated=None)
    )
    with pytest.raises(OrderTotalUpdateError):
        OrderService(customers, products, prices, orders, inventory).create_draft_order(
            ORDER_CREATE
        )

    assert tx.journal == ["rollback"]


# ---------------------------------------------------------------------------
# 17-18. connection and side-effect discipline
# ---------------------------------------------------------------------------


def test_all_repository_calls_share_one_connection(tx):
    customers, products, prices, orders, inventory = _world()
    OrderService(customers, products, prices, orders, inventory).create_draft_order(
        ORDER_CREATE
    )

    every_call = customers.calls + products.calls + prices.calls + orders.calls
    assert len(every_call) >= 8
    assert {call[1] for call in every_call} == {tx.conn}


def test_create_draft_touches_no_inventory(tx):
    # draft creation is side-effect free on stock: the inventory
    # repository is injected but never called by this workflow.
    customers, products, prices, orders, inventory = _world()
    OrderService(customers, products, prices, orders, inventory).create_draft_order(
        ORDER_CREATE
    )

    assert inventory.calls == []


def test_no_credit_limit_or_balance_mutation(tx):
    # the customer is only ever read: one get_by_id, no write calls,
    # so credit_limit and outstanding_balance stay untouched.
    customers, products, prices, orders, inventory = _world(
        customer=_customer(outstanding_balance=Decimal("777.77"))
    )
    OrderService(customers, products, prices, orders, inventory).create_draft_order(
        ORDER_CREATE
    )

    assert customers.calls == [("get_by_id", tx.conn, CUSTOMER_ID)]


def test_service_module_never_imports_connection_factory():
    # services coordinate; connection and transaction lifecycle stay in
    # app.database.connection
    assert "get_connection" not in vars(order_service_module)
    assert "connect" not in vars(order_service_module)
    assert "transaction" in vars(order_service_module)  # the approved boundary


# ===========================================================================
# confirm_order
# ===========================================================================
# World defaults: balance 500.00, confirmed totals 1000.00, order total
# 2800.00 -> projected exposure 4300.00 against a 10000.00 limit; stock
# on_hand 100/50 against quantities 2/1. Every number below is Decimal.


def _confirm_world(**overrides):
    """_world with confirm-correct reads: draft under lock, confirmed after.

    get_by_id serves both the preliminary read (customer_id only) and the
    final read-back, so detail is the CONFIRMED order; locked_detail is
    the DRAFT the service re-reads under the row lock.
    """
    overrides.setdefault(
        "orders",
        FakeOrderRepository(
            detail=_detail(_stored_items(), TOTAL, status=OrderStatus.CONFIRMED),
            locked_detail=_detail(_stored_items(), TOTAL),
            confirmed_total=Decimal("1000.00"),
            marked=_order(TOTAL, status=OrderStatus.CONFIRMED, confirmed_at=NOW),
        ),
    )
    return _world(**overrides)


def test_confirm_order_success(tx):
    customers, products, prices, orders, inventory = _confirm_world()
    result = OrderService(customers, products, prices, orders, inventory).confirm_order(
        ORDER_ID
    )

    assert isinstance(result, OrderDetailResponse)
    assert result is orders.detail
    assert result.status is OrderStatus.CONFIRMED
    assert tx.journal == ["commit"]


def test_confirm_order_workflow_call_sequence(tx):
    customers, products, prices, orders, inventory = _confirm_world()
    OrderService(customers, products, prices, orders, inventory).confirm_order(ORDER_ID)

    assert _names(orders) == [
        "get_by_id",  # unlocked preliminary read: customer_id only
        "get_for_update",  # locked re-read: the draft under the row lock
        "sum_confirmed_totals",  # credit exposure input
        "mark_confirmed",  # guarded transition
        "get_by_id",  # read back the confirmed detail
    ]
    assert _names(customers) == ["get_for_update"]  # locked, never get_by_id
    assert _names(inventory) == [
        "lock_by_product_ids",  # all rows, one statement
        "reserve_stock",
        "reserve_stock",
    ]


def test_confirm_order_marks_confirmed_with_guarded_update(tx):
    customers, products, prices, orders, inventory = _confirm_world()
    OrderService(customers, products, prices, orders, inventory).confirm_order(ORDER_ID)

    assert orders.calls[3] == ("mark_confirmed", tx.conn, ORDER_ID)


def test_confirm_order_reserves_exact_quantities(tx):
    customers, products, prices, orders, inventory = _confirm_world()
    OrderService(customers, products, prices, orders, inventory).confirm_order(ORDER_ID)

    assert inventory.reserved == {PRODUCT_ID: 2, OTHER_PRODUCT_ID: 1}


def test_confirm_order_locks_inventory_in_ascending_product_id(tx):
    customers, products, prices, orders, inventory = _confirm_world()
    OrderService(customers, products, prices, orders, inventory).confirm_order(ORDER_ID)

    lock_call = inventory.calls[0]
    assert lock_call[0] == "lock_by_product_ids"
    assert lock_call[1] is tx.conn
    # deterministic lock acquisition order (migration 001 deadlock rule)
    assert lock_call[2] == sorted([PRODUCT_ID, OTHER_PRODUCT_ID])


def test_confirm_order_on_hand_stock_unchanged(tx):
    customers, products, prices, orders, inventory = _confirm_world()
    OrderService(customers, products, prices, orders, inventory).confirm_order(ORDER_ID)

    # reservation only: physical stock is the delivery workflow's job
    assert inventory.inventory[PRODUCT_ID].on_hand_stock == 100
    assert inventory.inventory[OTHER_PRODUCT_ID].on_hand_stock == 50


def test_confirm_order_order_not_found(tx):
    customers, products, prices, orders, inventory = _world(
        orders=FakeOrderRepository(detail=None)
    )
    missing = uuid4()
    with pytest.raises(OrderNotFoundError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            missing
        )

    assert excinfo.value.order_id == missing
    assert tx.journal == ["rollback"]
    assert customers.calls == []  # no customer lock after the miss


def test_confirm_order_locked_order_missing(tx):
    # preliminary read found it, the locked re-read did not: deleted in
    # flight — same domain failure, raised after the customer lock
    customers, products, prices, orders, inventory = _world(
        orders=FakeOrderRepository(
            detail=_detail(_stored_items(), TOTAL), locked_detail=None
        )
    )
    with pytest.raises(OrderNotFoundError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.order_id == ORDER_ID
    assert tx.journal == ["rollback"]
    assert customers.calls == [("get_for_update", tx.conn, CUSTOMER_ID)]


def test_confirm_order_customer_not_found(tx):
    customers, products, prices, orders, inventory = _world(customer=None)
    with pytest.raises(CustomerNotFoundError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.customer_id == CUSTOMER_ID
    assert tx.journal == ["rollback"]
    assert _names(orders) == ["get_by_id"]  # only the preliminary read ran
    assert inventory.calls == []


def test_confirm_order_status_not_draft(tx):
    customers, products, prices, orders, inventory = _world(
        orders=FakeOrderRepository(
            detail=_detail(_stored_items(), TOTAL),
            locked_detail=_detail(_stored_items(), TOTAL, status=OrderStatus.DELIVERED),
        )
    )
    with pytest.raises(InvalidOrderStatusError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.order_id == ORDER_ID
    assert excinfo.value.current_status is OrderStatus.DELIVERED
    assert excinfo.value.target_status == "confirmed"
    assert tx.journal == ["rollback"]
    # no credit check, no reservation, no transition for a terminal order
    assert "sum_confirmed_totals" not in _names(orders)
    assert "mark_confirmed" not in _names(orders)
    assert inventory.calls == []


def test_confirm_order_empty_order_rejected(tx):
    customers, products, prices, orders, inventory = _world(
        orders=FakeOrderRepository(
            detail=_detail([], Decimal("0.00")),
            locked_detail=_detail([], Decimal("0.00")),
        )
    )
    with pytest.raises(EmptyOrderError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.order_id == ORDER_ID
    assert tx.journal == ["rollback"]
    assert "sum_confirmed_totals" not in _names(orders)
    assert inventory.calls == []


def test_confirm_order_mark_backstop_none(tx):
    # guarded UPDATE matched nothing (status changed despite the lock):
    # InvalidOrderStatusError, rollback discards the reservations above
    customers, products, prices, orders, inventory = _confirm_world(
        orders=FakeOrderRepository(
            detail=_detail(_stored_items(), TOTAL, status=OrderStatus.CONFIRMED),
            locked_detail=_detail(_stored_items(), TOTAL),
            confirmed_total=Decimal("1000.00"),
            marked=None,  # zero rows affected
        )
    )
    with pytest.raises(InvalidOrderStatusError):
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert tx.journal == ["rollback"]


def test_confirm_order_inactive_customer_still_confirmable(tx):
    # inactivity blocks draft CREATION, never confirmation of an
    # existing draft
    customers, products, prices, orders, inventory = _confirm_world(
        customer=_customer(is_active=False)
    )
    result = OrderService(customers, products, prices, orders, inventory).confirm_order(
        ORDER_ID
    )

    assert result is orders.detail
    assert tx.journal == ["commit"]
    assert inventory.reserved == {PRODUCT_ID: 2, OTHER_PRODUCT_ID: 1}


def test_confirm_order_inactive_product_still_confirmable(tx):
    # same rule for products: confirm reads no product rows at all
    customers, products, prices, orders, inventory = _confirm_world(
        products={
            PRODUCT_ID: _product(PRODUCT_ID),
            OTHER_PRODUCT_ID: _product(OTHER_PRODUCT_ID, is_active=False),
        }
    )
    result = OrderService(customers, products, prices, orders, inventory).confirm_order(
        ORDER_ID
    )

    assert result is orders.detail
    assert products.calls == []
    assert prices.calls == []  # prices are frozen in the order items
    assert tx.journal == ["commit"]


def test_confirm_order_credit_limit_not_configured(tx):
    # NULL = not configured (migration 002): reject, never "unlimited"
    customers, products, prices, orders, inventory = _confirm_world(
        customer=_customer(credit_limit=None)
    )
    with pytest.raises(CreditLimitNotConfiguredError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.customer_id == CUSTOMER_ID
    assert tx.journal == ["rollback"]
    assert "sum_confirmed_totals" not in _names(orders)
    assert inventory.calls == []


def test_confirm_order_zero_credit_limit_rejects_positive_order(tx):
    # 0 is a CONFIGURED limit: exposure 4300.00 > 0.00, CreditLimitExceeded,
    # not CreditLimitNotConfigured
    customers, products, prices, orders, inventory = _confirm_world(
        customer=_customer(credit_limit=Decimal("0.00"))
    )
    with pytest.raises(CreditLimitExceededError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.credit_limit == Decimal("0.00")
    assert excinfo.value.projected_exposure == Decimal("4300.00")
    assert tx.journal == ["rollback"]
    assert inventory.calls == []


def test_confirm_order_credit_exceeded(tx):
    # exposure 500.00 + 1000.00 + 2800.00 = 4300.00 > 4000.00
    customers, products, prices, orders, inventory = _confirm_world(
        customer=_customer(credit_limit=Decimal("4000.00"))
    )
    with pytest.raises(CreditLimitExceededError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.customer_id == CUSTOMER_ID
    assert excinfo.value.credit_limit == Decimal("4000.00")
    assert excinfo.value.projected_exposure == Decimal("4300.00")
    assert tx.journal == ["rollback"]
    assert inventory.calls == []  # no reservation when credit fails
    assert "mark_confirmed" not in _names(orders)


def test_confirm_order_exposure_exactly_at_limit_allowed(tx):
    # projected exposure 4300.00 == limit 4300.00: allowed (<=, not <)
    customers, products, prices, orders, inventory = _confirm_world(
        customer=_customer(credit_limit=Decimal("4300.00"))
    )
    result = OrderService(customers, products, prices, orders, inventory).confirm_order(
        ORDER_ID
    )

    assert result is orders.detail
    assert tx.journal == ["commit"]


def test_confirm_order_exposure_at_limit_new_order_exceeds(tx):
    # standing exposure 500.00 + 9500.00 = limit; the draft's 2800.00
    # pushes projected to 12800.00 > 10000.00
    customers, products, prices, orders, inventory = _confirm_world(
        orders=FakeOrderRepository(
            detail=_detail(_stored_items(), TOTAL, status=OrderStatus.CONFIRMED),
            locked_detail=_detail(_stored_items(), TOTAL),
            confirmed_total=Decimal("9500.00"),
            marked=_order(TOTAL, status=OrderStatus.CONFIRMED, confirmed_at=NOW),
        )
    )
    with pytest.raises(CreditLimitExceededError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.credit_limit == Decimal("10000.00")
    assert excinfo.value.projected_exposure == Decimal("12800.00")
    assert tx.journal == ["rollback"]
    assert inventory.calls == []


def test_confirm_order_inventory_row_missing(tx):
    customers, products, prices, orders, inventory = _confirm_world(
        inventory=FakeInventoryRepository({PRODUCT_ID: _inventory(PRODUCT_ID, 100)})
    )
    with pytest.raises(InventoryNotFoundError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.product_id == OTHER_PRODUCT_ID
    assert tx.journal == ["rollback"]
    assert inventory.reserved == {}  # missing row: nothing reserved
    assert "mark_confirmed" not in _names(orders)


def test_confirm_order_insufficient_stock_rejects_entire_order(tx):
    # first item has stock; second does not: all-or-nothing means the
    # first item must NOT be reserved either
    customers, products, prices, orders, inventory = _confirm_world(
        inventory=FakeInventoryRepository(
            {
                PRODUCT_ID: _inventory(PRODUCT_ID, 100),
                OTHER_PRODUCT_ID: _inventory(OTHER_PRODUCT_ID, 0),
            }
        )
    )
    with pytest.raises(InsufficientStockError) as excinfo:
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert excinfo.value.product_id == OTHER_PRODUCT_ID
    assert excinfo.value.requested == 1
    assert excinfo.value.available == 0
    assert tx.journal == ["rollback"]
    assert inventory.reserved == {}
    assert [c for c in inventory.calls if c[0] == "reserve_stock"] == []
    assert "mark_confirmed" not in _names(orders)


def test_confirm_order_reserves_only_after_all_items_validated(tx):
    # validation of every item precedes the first update: the lock call
    # is the only inventory call that may precede a failure
    customers, products, prices, orders, inventory = _confirm_world(
        inventory=FakeInventoryRepository(
            {
                PRODUCT_ID: _inventory(PRODUCT_ID, 1),  # would suffice alone...
                OTHER_PRODUCT_ID: _inventory(OTHER_PRODUCT_ID, 0),  # ...but fails
            }
        )
    )
    with pytest.raises(InsufficientStockError):
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert _names(inventory) == ["lock_by_product_ids"]


def test_confirm_order_reserve_error_propagates(tx):
    # psycopg errors are never swallowed into business exceptions
    customers, products, prices, orders, inventory = _confirm_world(
        inventory=FakeInventoryRepository(
            {
                PRODUCT_ID: _inventory(PRODUCT_ID, 100),
                OTHER_PRODUCT_ID: _inventory(OTHER_PRODUCT_ID, 50),
            },
            reserve_error=psycopg.OperationalError(),
        )
    )
    with pytest.raises(psycopg.OperationalError):
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert tx.journal == ["rollback"]


def test_confirm_order_mark_error_propagates(tx):
    customers, products, prices, orders, inventory = _confirm_world(
        orders=FakeOrderRepository(
            detail=_detail(_stored_items(), TOTAL, status=OrderStatus.CONFIRMED),
            locked_detail=_detail(_stored_items(), TOTAL),
            confirmed_total=Decimal("1000.00"),
            mark_error=psycopg.OperationalError(),
        )
    )
    with pytest.raises(psycopg.OperationalError):
        OrderService(customers, products, prices, orders, inventory).confirm_order(
            ORDER_ID
        )

    assert tx.journal == ["rollback"]
    # reservations happened, then the failure rolled them back with the
    # rest of the workflow
    assert _names(inventory) == [
        "lock_by_product_ids",
        "reserve_stock",
        "reserve_stock",
    ]


def test_confirm_order_all_calls_share_one_connection(tx):
    customers, products, prices, orders, inventory = _confirm_world()
    OrderService(customers, products, prices, orders, inventory).confirm_order(ORDER_ID)

    every_call = customers.calls + orders.calls + inventory.calls
    assert len(every_call) >= 8
    assert {call[1] for call in every_call} == {tx.conn}


def test_confirm_order_customer_only_read_balance_never_mutated(tx):
    customers, products, prices, orders, inventory = _confirm_world(
        customer=_customer(outstanding_balance=Decimal("777.77"))
    )
    OrderService(customers, products, prices, orders, inventory).confirm_order(ORDER_ID)

    # the customer row is locked and read, never written: the fake has
    # no write methods, and the only recorded call is the lock read
    assert customers.calls == [("get_for_update", tx.conn, CUSTOMER_ID)]
    assert tx.journal == ["commit"]
