"""ProductInquiryService unit tests — mocked repositories and transaction.

Under test: the search_products workflow (customer validation, shared
price-list resolution, exact-SKU-priority search policy, per-product price
and inventory resolution, error-instead-of-default rules) and the
read-only contract (no writes, one connection per repository call).

Case-insensitive SKU matching and inactive filtering are SQL behavior —
they are integration-tested live; here the fakes mirror the SQL contract
(lower() key matching, active-only results) so the service's delegation is
what gets checked.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

import app.services.product_inquiry as inquiry_service_module
from app.schemas.customer import CustomerResponse
from app.schemas.inventory import InventoryResponse
from app.schemas.price_list import PriceListResponse, ProductPriceResponse
from app.schemas.product import ProductResponse
from app.schemas.product_inquiry import (
    ProductInquiryItem,
    ProductInquiryRequest,
    ProductInquiryResponse,
)
from app.services.exceptions import (
    CustomerNotFoundError,
    InventoryNotFoundError,
    PriceListNotFoundError,
    ProductPriceNotFoundError,
)
from app.services.price_list_resolver import GENERAL_PRICE_LIST_CODE
from app.services.product_inquiry import ProductInquiryService

NOW = datetime.now(UTC)
CUSTOMER_ID = uuid4()
CUSTOMER_LIST_ID = uuid4()
GENERAL_LIST_ID = uuid4()
PRODUCT_ID = uuid4()
OTHER_PRODUCT_ID = uuid4()

PRICE = Decimal("150.00")
OTHER_PRICE = Decimal("2500.00")


def _customer(
    *,
    price_list_id: UUID | None = CUSTOMER_LIST_ID,
    is_active: bool = True,
) -> CustomerResponse:
    return CustomerResponse(
        id=CUSTOMER_ID,
        code="CUST-001",
        name="Toko Sumber Makmur",
        phone="+628123456789",
        address="Jl. Merdeka 1",
        credit_limit=Decimal("10000.00"),
        outstanding_balance=Decimal("500.00"),
        price_list_id=price_list_id,
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


def _inventory(product_id: UUID, on_hand: int, reserved: int = 0) -> InventoryResponse:
    return InventoryResponse(
        id=uuid4(),
        product_id=product_id,
        on_hand_stock=on_hand,
        reserved_stock=reserved,
        available_stock=on_hand - reserved,  # as the generated column returns it
        updated_at=NOW,
    )


class FakeConn:
    """Connection identity token — recorded by every fake repository."""


class FakeTransaction:
    """Stands in for app.database.connection.transaction()."""

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
    def __init__(self, customer: CustomerResponse | None) -> None:
        self.customer = customer
        self.calls: list[tuple] = []

    def get_by_id(self, conn, customer_id):
        self.calls.append(("get_by_id", conn, customer_id))
        return self.customer


class FakeProductRepository:
    """Mirrors the SQL contract of search_by_exact_sku: keys are stored
    lowercased so lookups are case-insensitive, exact (no wildcards),
    active products only."""

    def __init__(
        self,
        exact_sku: dict[str, list[ProductResponse]] | None = None,
        name_or_alias: list[ProductResponse] | None = None,
    ) -> None:
        self.exact_sku = {
            sku.lower(): products for sku, products in (exact_sku or {}).items()
        }
        self.name_or_alias = name_or_alias or []
        self.calls: list[tuple] = []

    def search_by_exact_sku(self, conn, sku):
        self.calls.append(("search_by_exact_sku", conn, sku))
        return self.exact_sku.get(sku.lower(), [])

    def search_by_name_or_alias(self, conn, query):
        self.calls.append(("search_by_name_or_alias", conn, query))
        return self.name_or_alias


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


class FakeInventoryRepository:
    def __init__(
        self, inventories: dict[UUID, InventoryResponse | None] | None = None
    ) -> None:
        self.inventories = inventories or {}
        self.calls: list[tuple] = []

    def get_by_product_id(self, conn, product_id):
        self.calls.append(("get_by_product_id", conn, product_id))
        return self.inventories.get(product_id)


def _product(
    product_id: UUID,
    sku: str,
    name: str = "Aqua Botol 600ml",
    *,
    is_active: bool = True,
) -> ProductResponse:
    return ProductResponse(
        id=product_id,
        sku=sku,
        name=name,
        unit="pcs",
        is_active=is_active,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def tx(monkeypatch) -> FakeTransaction:
    fake = FakeTransaction()
    monkeypatch.setattr(inquiry_service_module, "transaction", fake)
    return fake


_UNSET = object()


def _world(
    *,
    customer: CustomerResponse | None | object = _UNSET,
    products: FakeProductRepository | None = None,
    price_lists_by_id: dict[UUID, PriceListResponse] | None = None,
    price_lists_by_code: dict[str, PriceListResponse] | None = None,
    product_prices: dict[tuple[UUID, UUID], ProductPriceResponse] | None = None,
    inventories: dict[UUID, InventoryResponse] | None = None,
):
    """All fakes default to the happy path: one priced, stocked product.

    `is None` / sentinel checks (never `or`): explicit falsy overrides
    (customer=None, empty dicts) must reach the fakes unchanged.
    """
    if customer is _UNSET:
        customer = _customer()
    if products is None:
        products = FakeProductRepository(
            exact_sku={"AQUA-600ML": [_product(PRODUCT_ID, "AQUA-600ML")]}
        )
    if price_lists_by_id is None:
        price_lists_by_id = {CUSTOMER_LIST_ID: _price_list(CUSTOMER_LIST_ID)}
    if price_lists_by_code is None:
        price_lists_by_code = {GENERAL_PRICE_LIST_CODE: _price_list(GENERAL_LIST_ID)}
    if product_prices is None:
        product_prices = {
            (CUSTOMER_LIST_ID, PRODUCT_ID): _product_price(
                CUSTOMER_LIST_ID, PRODUCT_ID, PRICE
            )
        }
    if inventories is None:
        inventories = {PRODUCT_ID: _inventory(PRODUCT_ID, 100)}
    customers = FakeCustomerRepository(customer=customer)
    prices = FakePriceRepository(
        price_lists_by_id=price_lists_by_id,
        price_lists_by_code=price_lists_by_code,
        product_prices=product_prices,
    )
    inventory = FakeInventoryRepository(inventories=inventories)
    return customers, products, prices, inventory


def _service(
    customers: FakeCustomerRepository,
    products: FakeProductRepository,
    prices: FakePriceRepository,
    inventory: FakeInventoryRepository,
) -> ProductInquiryService:
    return ProductInquiryService(customers, products, prices, inventory)


def _request(
    term: str = "aqua-600ml", customer_id: UUID = CUSTOMER_ID
) -> ProductInquiryRequest:
    return ProductInquiryRequest(customer_id=customer_id, search_term=term)


def test_unknown_customer_raises(tx):
    customers, products, prices, inventory = _world(customer=None)
    with pytest.raises(CustomerNotFoundError):
        _service(customers, products, prices, inventory).search_products(_request())
    assert tx.journal == ["rollback"]
    # GENERAL never consulted: pricing fallback is for lists, not customers
    assert (
        "get_price_list_by_code",
        tx.conn,
        GENERAL_PRICE_LIST_CODE,
    ) not in prices.calls


def test_assigned_customer_price_list_used(tx):
    customers, products, prices, inventory = _world()
    result = _service(customers, products, prices, inventory).search_products(
        _request()
    )
    assert ("get_price_list_by_id", tx.conn, CUSTOMER_LIST_ID) in prices.calls
    assert (
        "get_price_list_by_code",
        tx.conn,
        GENERAL_PRICE_LIST_CODE,
    ) not in prices.calls
    assert result.products[0].unit_price == PRICE


def test_null_customer_price_list_resolves_general(tx):
    customers, products, prices, inventory = _world(
        customer=_customer(price_list_id=None)
    )
    general_price = {
        (GENERAL_LIST_ID, PRODUCT_ID): _product_price(
            GENERAL_LIST_ID, PRODUCT_ID, PRICE
        )
    }
    prices.prices = general_price
    result = _service(customers, products, prices, inventory).search_products(
        _request()
    )
    assert ("get_price_list_by_code", tx.conn, GENERAL_PRICE_LIST_CODE) in prices.calls
    assert result.products[0].unit_price == PRICE


def test_assigned_price_list_missing_raises(tx):
    customers, products, prices, inventory = _world(price_lists_by_id={})
    with pytest.raises(PriceListNotFoundError):
        _service(customers, products, prices, inventory).search_products(_request())
    assert tx.journal == ["rollback"]


def test_exact_sku_lookup_is_case_insensitive(tx):
    customers, products, prices, inventory = _world()
    # stored SKU upper, term mixed case -> match via lower() key lookup
    result = _service(customers, products, prices, inventory).search_products(
        _request("AqUa-600Ml")
    )
    assert [p.sku for p in result.products] == ["AQUA-600ML"]
    assert ("search_by_exact_sku", tx.conn, "AqUa-600Ml") in products.calls


def test_exact_sku_match_priority_over_broader_search(tx):
    sku_product = _product(PRODUCT_ID, "AQUA-600ML")
    name_product = _product(OTHER_PRODUCT_ID, "OTHER-SKU", name="Aqua Botol 600ml")
    products = FakeProductRepository(
        exact_sku={"AQUA-600ML": [sku_product]},
        name_or_alias=[sku_product, name_product],
    )
    prices = FakePriceRepository(
        price_lists_by_id={CUSTOMER_LIST_ID: _price_list(CUSTOMER_LIST_ID)},
        product_prices={
            (CUSTOMER_LIST_ID, PRODUCT_ID): _product_price(
                CUSTOMER_LIST_ID, PRODUCT_ID, PRICE
            ),
            (CUSTOMER_LIST_ID, OTHER_PRODUCT_ID): _product_price(
                CUSTOMER_LIST_ID, OTHER_PRODUCT_ID, OTHER_PRICE
            ),
        },
    )
    inventory = FakeInventoryRepository(
        inventories={
            PRODUCT_ID: _inventory(PRODUCT_ID, 100),
            OTHER_PRODUCT_ID: _inventory(OTHER_PRODUCT_ID, 50),
        }
    )
    customers = FakeCustomerRepository(_customer())
    result = _service(customers, products, prices, inventory).search_products(
        _request()
    )
    # SKU match returned alone; name search never ran
    assert [p.sku for p in result.products] == ["AQUA-600ML"]
    assert all(call[0] != "search_by_name_or_alias" for call in products.calls)


def test_name_search_works_when_no_sku_match(tx):
    name_product = _product(PRODUCT_ID, "AQUA-600ML")
    products = FakeProductRepository(name_or_alias=[name_product])
    customers, _, prices, inventory = _world()
    result = _service(customers, products, prices, inventory).search_products(
        _request("aqua botol")
    )
    assert [p.sku for p in result.products] == ["AQUA-600ML"]
    assert ("search_by_name_or_alias", tx.conn, "aqua botol") in products.calls


def test_alias_search_works(tx):
    # alias hits land in the same name/alias search; the term is passed
    # through unchanged and the matched product is priced and stocked
    alias_product = _product(PRODUCT_ID, "AQUA-600ML")
    products = FakeProductRepository(name_or_alias=[alias_product])
    customers, _, prices, inventory = _world()
    result = _service(customers, products, prices, inventory).search_products(
        _request("aqua botol")
    )
    assert result.products[0].product_id == PRODUCT_ID


def test_inactive_products_not_returned(tx):
    # repo contract: exact-sku and name searches return active rows only;
    # the service adds no inactive filter of its own (none needed)
    active = _product(PRODUCT_ID, "AQUA-600ML")
    inactive = _product(OTHER_PRODUCT_ID, "OTHER-SKU", is_active=False)
    products = FakeProductRepository(
        exact_sku={},  # active-only contract: inactive SKU never surfaces
        name_or_alias=[active],  # inactive name match filtered by SQL
    )
    assert inactive.is_active is False
    customers, _, prices, inventory = _world()
    result = _service(customers, products, prices, inventory).search_products(
        _request("aqua botol")
    )
    assert [p.product_id for p in result.products] == [PRODUCT_ID]


def test_missing_product_price_raises(tx):
    customers, products, prices, inventory = _world(product_prices={})
    with pytest.raises(ProductPriceNotFoundError) as excinfo:
        _service(customers, products, prices, inventory).search_products(_request())
    assert excinfo.value.price_list_id == CUSTOMER_LIST_ID
    assert tx.journal == ["rollback"]
    # no GENERAL fallback attempted for the missing product price
    assert (
        "get_price_list_by_code",
        tx.conn,
        GENERAL_PRICE_LIST_CODE,
    ) not in prices.calls


def test_missing_inventory_raises(tx):
    customers, products, prices, inventory = _world(inventories={})
    with pytest.raises(InventoryNotFoundError):
        _service(customers, products, prices, inventory).search_products(_request())
    assert tx.journal == ["rollback"]


def test_zero_available_stock_returns_zero(tx):
    customers, products, prices, inventory = _world(
        inventories={PRODUCT_ID: _inventory(PRODUCT_ID, 0)}
    )
    result = _service(customers, products, prices, inventory).search_products(
        _request()
    )
    assert result.products[0].available_stock == 0


def test_no_match_returns_empty_list(tx):
    products = FakeProductRepository()
    customers, _, prices, inventory = _world(products=products)
    result = _service(customers, products, prices, inventory).search_products(
        _request("zzznomatch")
    )
    assert result.products == []


def test_multiple_matches_return_multiple_candidates(tx):
    first = _product(PRODUCT_ID, "AQUA-600ML")
    second = _product(OTHER_PRODUCT_ID, "AQUA-1L", name="Aqua Botol 1L")
    products = FakeProductRepository(name_or_alias=[first, second])
    customers, _, prices, inventory = _world(
        products=products,
        product_prices={
            (CUSTOMER_LIST_ID, PRODUCT_ID): _product_price(
                CUSTOMER_LIST_ID, PRODUCT_ID, PRICE
            ),
            (CUSTOMER_LIST_ID, OTHER_PRODUCT_ID): _product_price(
                CUSTOMER_LIST_ID, OTHER_PRODUCT_ID, OTHER_PRICE
            ),
        },
        inventories={
            PRODUCT_ID: _inventory(PRODUCT_ID, 100),
            OTHER_PRODUCT_ID: _inventory(OTHER_PRODUCT_ID, 50),
        },
    )
    result = _service(customers, products, prices, inventory).search_products(
        _request("aqua botol")
    )
    assert [p.sku for p in result.products] == ["AQUA-600ML", "AQUA-1L"]
    assert result.products[1].unit_price == OTHER_PRICE


def test_response_shape(tx):
    customers, products, prices, inventory = _world()
    result = _service(customers, products, prices, inventory).search_products(
        _request()
    )
    assert isinstance(result, ProductInquiryResponse)
    item = result.products[0]
    assert isinstance(item, ProductInquiryItem)
    assert item.product_id == PRODUCT_ID
    assert item.sku == "AQUA-600ML"
    assert item.name == "Aqua Botol 600ml"
    assert item.unit_price == Decimal("150.00")
    assert item.available_stock == 100


def test_all_repository_calls_share_one_connection(tx):
    customers, products, prices, inventory = _world()
    _service(customers, products, prices, inventory).search_products(_request())
    all_calls = customers.calls + products.calls + prices.calls + inventory.calls
    assert all(conn is tx.conn for _, conn, *_ in all_calls)


def test_success_commits_and_is_read_only(tx):
    customers, products, prices, inventory = _world()
    _service(customers, products, prices, inventory).search_products(_request())
    assert tx.journal == ["commit"]
    # no repository exposes a write method to call — read-only contract;
    # inventory is only ever read via get_by_product_id
    assert all(call[0] == "get_by_product_id" for call in inventory.calls)


def test_service_module_never_imports_connection_factory():
    # services coordinate; connection and transaction lifecycle stay in
    # app.database.connection
    assert "get_connection" not in vars(inquiry_service_module)
    assert "connect" not in vars(inquiry_service_module)
    assert "transaction" in vars(inquiry_service_module)  # the approved boundary
