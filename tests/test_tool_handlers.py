"""Tool handler tests, fake services, no database.

Under test: request translation (validated args + trusted context →
internal service request), response translation (service response →
model-safe payload with money as exact strings), the confirmation
guards (approval, ownership), and error propagation to the executor.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.schemas.order import OrderDetailResponse, OrderItemResponse, OrderStatus
from app.schemas.product_inquiry import (
    ProductInquiryItem,
    ProductInquiryRequest,
    ProductInquiryResponse,
)
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
    ServiceError,
)
from app.tools.arguments import (
    ConfirmOrderArgs,
    CreateDraftOrderArgs,
    SearchProductsArgs,
)
from app.tools.context import ToolContext
from app.tools.exceptions import ToolHandlerError
from app.tools.handlers import (
    ToolServices,
    execute_confirm_order,
    execute_create_draft_order,
    execute_search_products,
    map_service_error,
)

NOW = datetime.now(UTC)
CUSTOMER_ID = uuid4()
OTHER_CUSTOMER_ID = uuid4()
PRODUCT_ID = uuid4()
ORDER_ID = uuid4()


class FakeProductInquiryService:
    def __init__(
        self,
        response: ProductInquiryResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or ProductInquiryResponse(products=[])
        self.error = error
        self.requests: list[ProductInquiryRequest] = []

    def search_products(self, request: ProductInquiryRequest) -> ProductInquiryResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class FakeOrderService:
    def __init__(
        self,
        draft: OrderDetailResponse | None = None,
        confirmed: OrderDetailResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.draft = draft
        self.confirmed = confirmed
        self.error = error
        self.create_requests: list = []
        self.get_order_ids: list[UUID] = []
        self.confirm_ids: list[UUID] = []

    def create_draft_order(self, order):
        self.create_requests.append(order)
        if self.error is not None:
            raise self.error
        return self.draft

    def get_order(self, order_id: UUID) -> OrderDetailResponse:
        self.get_order_ids.append(order_id)
        if self.draft is None:
            raise OrderNotFoundError(order_id)
        return self.draft

    def confirm_order(self, order_id: UUID) -> OrderDetailResponse:
        self.confirm_ids.append(order_id)
        if self.error is not None:
            raise self.error
        return self.confirmed


def _services(
    inquiry: FakeProductInquiryService | None = None,
    orders: FakeOrderService | None = None,
) -> ToolServices:
    return ToolServices(
        product_inquiry=inquiry or FakeProductInquiryService(),
        orders=orders or FakeOrderService(),
    )


def _inquiry_response() -> ProductInquiryResponse:
    return ProductInquiryResponse(
        products=[
            ProductInquiryItem(
                product_id=PRODUCT_ID,
                sku="AQUA600",
                name="Aqua 600 ml",
                unit_price=Decimal("150.00"),
                available_stock=120,
            )
        ]
    )


def _order_detail(
    *, status: OrderStatus = OrderStatus.DRAFT, customer_id: UUID = CUSTOMER_ID
) -> OrderDetailResponse:
    return OrderDetailResponse(
        id=ORDER_ID,
        order_number="ORD-00000042",
        customer_id=customer_id,
        status=status,
        total_amount=Decimal("1500.00"),
        notes="reguler",
        confirmed_at=NOW if status is OrderStatus.CONFIRMED else None,
        cancelled_at=None,
        delivered_at=None,
        created_at=NOW,
        updated_at=NOW,
        items=[
            OrderItemResponse(
                id=uuid4(),
                order_id=ORDER_ID,
                product_id=PRODUCT_ID,
                quantity=10,
                unit_price=Decimal("150.00"),
                subtotal=Decimal("1500.00"),
                created_at=NOW,
            )
        ],
    )


def _context(approved: bool = False) -> ToolContext:
    context = ToolContext(customer_id=CUSTOMER_ID, conversation_id="wa-1")
    return context.with_confirm_order_approval(ORDER_ID) if approved else context


# ---------------------------------------------------------------------------
# Layer discipline: handlers own translation, nothing else
# ---------------------------------------------------------------------------


def test_handlers_module_never_imports_database_layer():
    from pathlib import Path

    import app.tools.handlers as handlers_module

    source = Path(handlers_module.__file__).read_text()
    for banned in ("psycopg", "app.repositories", "app.database", "transaction("):
        assert banned not in source, banned
    # services are the only data path
    assert "ProductInquiryService" in source and "OrderService" in source


# ---------------------------------------------------------------------------
# search_products
# ---------------------------------------------------------------------------


def test_search_products_builds_request_from_context_and_args():
    inquiry = FakeProductInquiryService(_inquiry_response())
    services = _services(inquiry=inquiry)

    data = execute_search_products(
        SearchProductsArgs(search_term="Aqua 600 ml"), _context(), services
    )

    request = inquiry.requests[0]
    assert request.customer_id == CUSTOMER_ID  # from trusted context, not args
    assert request.search_term == "Aqua 600 ml"
    assert data == {
        "products": [
            {
                "product_id": str(PRODUCT_ID),
                "sku": "AQUA600",
                "name": "Aqua 600 ml",
                "unit_price": "150.00",  # Decimal as exact string
                "available_stock": 120,
            }
        ]
    }


def test_search_products_empty_result_is_an_empty_list():
    data = execute_search_products(
        SearchProductsArgs(search_term="tidak ada"), _context(), _services()
    )
    assert data == {"products": []}


# ---------------------------------------------------------------------------
# create_draft_order
# ---------------------------------------------------------------------------


def test_create_draft_order_builds_order_create_with_context_customer():
    orders = FakeOrderService(draft=_order_detail())
    services = _services(orders=orders)

    execute_create_draft_order(
        CreateDraftOrderArgs(
            items=[{"product_id": str(PRODUCT_ID), "quantity": 10}], notes="reguler"
        ),
        _context(),
        services,
    )

    order = orders.create_requests[0]
    assert order.customer_id == CUSTOMER_ID  # trusted, never model-supplied
    assert order.notes == "reguler"
    assert order.items[0].product_id == PRODUCT_ID
    assert order.items[0].quantity == 10
    assert (
        "unit_price" not in type(order.items[0]).model_fields
    )  # args carry no price path


def test_create_draft_order_payload_is_model_safe():
    orders = FakeOrderService(draft=_order_detail())
    data = execute_create_draft_order(
        CreateDraftOrderArgs(items=[{"product_id": str(PRODUCT_ID), "quantity": 10}]),
        _context(),
        _services(orders=orders),
    )
    assert data["order_id"] == str(ORDER_ID)
    assert data["order_number"] == "ORD-00000042"
    assert data["status"] == "draft"
    assert data["total_amount"] == "1500.00"
    assert data["items"][0]["unit_price"] == "150.00"
    assert data["items"][0]["subtotal"] == "1500.00"
    assert data["confirmed_at"] is None


def test_create_draft_order_service_error_propagates_raw():
    orders = FakeOrderService(
        error=InsufficientStockError(PRODUCT_ID, 10, 5),
        draft=_order_detail(),
    )
    with pytest.raises(InsufficientStockError):
        execute_create_draft_order(
            CreateDraftOrderArgs(
                items=[{"product_id": str(PRODUCT_ID), "quantity": 10}]
            ),
            _context(),
            _services(orders=orders),
        )


# ---------------------------------------------------------------------------
# confirm_order guards
# ---------------------------------------------------------------------------


def test_confirm_order_refuses_without_explicit_approval():
    orders = FakeOrderService(draft=_order_detail())
    with pytest.raises(ToolHandlerError) as excinfo:
        execute_confirm_order(
            ConfirmOrderArgs(order_id=ORDER_ID), _context(), _services(orders=orders)
        )
    assert excinfo.value.code == "unauthorized"
    # nothing was read or confirmed: approval fails before any service call
    assert orders.get_order_ids == []
    assert orders.confirm_ids == []


def test_confirm_order_refuses_draft_owned_by_other_customer():
    orders = FakeOrderService(draft=_order_detail(customer_id=OTHER_CUSTOMER_ID))
    with pytest.raises(ToolHandlerError) as excinfo:
        execute_confirm_order(
            ConfirmOrderArgs(order_id=ORDER_ID),
            _context(approved=True),
            _services(orders=orders),
        )
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.detail == {"order_id": str(ORDER_ID)}
    assert orders.get_order_ids == [ORDER_ID]  # ownership was checked
    assert orders.confirm_ids == []  # confirmation never ran


def test_confirm_order_refuses_order_other_than_the_approved_one():
    # approval for draft A must not authorize confirming draft B
    other_order_id = uuid4()
    orders = FakeOrderService(draft=_order_detail())
    with pytest.raises(ToolHandlerError) as excinfo:
        execute_confirm_order(
            ConfirmOrderArgs(order_id=other_order_id),
            _context(approved=True),  # approval names ORDER_ID
            _services(orders=orders),
        )
    assert excinfo.value.code == "unauthorized"
    assert excinfo.value.detail == {"order_id": str(other_order_id)}
    assert orders.get_order_ids == []  # refused before any service call
    assert orders.confirm_ids == []


def test_confirm_order_success_with_approval_and_ownership():
    orders = FakeOrderService(
        draft=_order_detail(),
        confirmed=_order_detail(status=OrderStatus.CONFIRMED),
    )
    data = execute_confirm_order(
        ConfirmOrderArgs(order_id=ORDER_ID),
        _context(approved=True),
        _services(orders=orders),
    )
    assert orders.get_order_ids == [ORDER_ID]
    assert orders.confirm_ids == [ORDER_ID]
    assert data["status"] == "confirmed"
    assert data["confirmed_at"] == NOW.isoformat()


def test_confirm_order_missing_draft_propagates_not_found():
    orders = FakeOrderService(draft=None)
    with pytest.raises(OrderNotFoundError):
        execute_confirm_order(
            ConfirmOrderArgs(order_id=ORDER_ID),
            _context(approved=True),
            _services(orders=orders),
        )
    assert orders.confirm_ids == []


# ---------------------------------------------------------------------------
# map_service_error exhaustiveness
# ---------------------------------------------------------------------------


def _every_service_error_instance() -> list[ServiceError]:
    """One instance of every ServiceError subclass declared today."""
    return [
        CustomerNotFoundError(CUSTOMER_ID),
        InactiveCustomerError(CUSTOMER_ID),
        PriceListNotFoundError("daftar-utama"),
        ProductNotFoundError(PRODUCT_ID),
        InactiveProductError(PRODUCT_ID),
        ProductPriceNotFoundError(PRODUCT_ID, uuid4()),
        OrderNotFoundError(ORDER_ID),
        OrderTotalUpdateError(ORDER_ID),
        InvalidOrderStatusError(ORDER_ID, "draft", "confirmed"),
        EmptyOrderError(ORDER_ID),
        CreditLimitNotConfiguredError(CUSTOMER_ID),
        CreditLimitExceededError(CUSTOMER_ID, Decimal("100"), Decimal("150")),
        InventoryNotFoundError(PRODUCT_ID),
        InsufficientStockError(PRODUCT_ID, 10, 5),
    ]


def _all_subclasses(root: type[ServiceError]) -> set[type[ServiceError]]:
    found: set[type[ServiceError]] = set()
    frontier: list[type[ServiceError]] = [root]
    while frontier:
        cls = frontier.pop()
        for child in cls.__subclasses__():
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


def test_map_service_error_leaves_no_silent_generic_fallback():
    # every business failure reaches the model with its own category; a
    # new subclass must be mapped in handlers, never silently generic
    for exc in _every_service_error_instance():
        mapped = map_service_error(exc)
        assert (mapped.code, mapped.message) != (
            "execution_error",
            "Operasi bisnis gagal.",
        ), type(exc).__name__


def test_every_service_error_subclass_is_accounted_for():
    listed = {type(exc).__name__ for exc in _every_service_error_instance()}
    declared = {cls.__name__ for cls in _all_subclasses(ServiceError)}
    assert listed == declared  # extend the instance list for new subclasses
