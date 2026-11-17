"""ToolExecutor tests, the full dispatch pipeline with fake services.

Under test: registry → argument validation → handler → service →
structured ToolResult, including every failure category the model can
receive (unknown tool, malformed JSON, invalid arguments, unauthorized,
mapped service errors, sanitized execution errors) and the
tool_call_id ↔ result association the conversation loop relies on.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.agents.schemas import AgentToolCall
from app.schemas.order import OrderDetailResponse, OrderStatus
from app.schemas.product_inquiry import (
    ProductInquiryItem,
    ProductInquiryResponse,
)
from app.services.exceptions import (
    CreditLimitExceededError,
    InsufficientStockError,
    OrderNotFoundError,
)
from app.tools.context import ToolContext
from app.tools.definitions import SEARCH_PRODUCTS_TOOL
from app.tools.exceptions import MissingToolHandlerError
from app.tools.executor import ToolExecutor, build_tool_executor
from app.tools.handlers import ToolServices
from app.tools.registry import ToolRegistry
from app.tools.results import ToolResult

NOW = datetime.now(UTC)
CUSTOMER_ID = uuid4()
PRODUCT_ID = uuid4()
ORDER_ID = uuid4()
CALL_ID = "call_1"


class FakeProductInquiryService:
    def __init__(
        self,
        response: ProductInquiryResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response or ProductInquiryResponse(products=[])
        self.error = error

    def search_products(self, request):
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
        self.confirm_ids = []
        self.get_order_ids = []

    def create_draft_order(self, order):
        if self.error is not None:
            raise self.error
        return self.draft

    def get_order(self, order_id):
        self.get_order_ids.append(order_id)
        if self.draft is None:
            raise OrderNotFoundError(order_id)
        return self.draft

    def confirm_order(self, order_id):
        self.confirm_ids.append(order_id)
        if self.error is not None:
            raise self.error
        return self.confirmed


def _order_detail(
    *, status: OrderStatus = OrderStatus.DRAFT, customer_id=CUSTOMER_ID
) -> OrderDetailResponse:
    return OrderDetailResponse(
        id=ORDER_ID,
        order_number="ORD-00000042",
        customer_id=customer_id,
        status=status,
        total_amount=Decimal("1500.00"),
        notes=None,
        confirmed_at=NOW if status is OrderStatus.CONFIRMED else None,
        cancelled_at=None,
        delivered_at=None,
        created_at=NOW,
        updated_at=NOW,
        items=[],
    )


def _executor(
    inquiry: FakeProductInquiryService | None = None,
    orders: FakeOrderService | None = None,
) -> ToolExecutor:
    return build_tool_executor(
        ToolServices(
            product_inquiry=inquiry or FakeProductInquiryService(),
            orders=orders or FakeOrderService(),
        )
    )


def _call(name: str, arguments: dict | str, call_id: str = CALL_ID) -> AgentToolCall:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return AgentToolCall(id=call_id, name=name, arguments=raw)


def _context(approved: bool = False) -> ToolContext:
    context = ToolContext(customer_id=CUSTOMER_ID)
    return context.with_confirm_order_approval() if approved else context


def _execute(
    executor: ToolExecutor, call: AgentToolCall, context: ToolContext
) -> ToolResult:
    return executor.execute(call.id, call.name, call.arguments, context)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_default_executor_exposes_three_executable_tools():
    executor = _executor()
    assert [spec["function"]["name"] for spec in executor.openai_tools()] == [
        "search_products",
        "create_draft_order",
        "confirm_order",
    ]


def test_handlerless_definition_fails_loud_at_lookup():
    registry = ToolRegistry()
    registry.register(SEARCH_PRODUCTS_TOOL)  # definition without handler
    executor = ToolExecutor(
        ToolServices(
            product_inquiry=FakeProductInquiryService(), orders=FakeOrderService()
        ),
        registry,
    )
    with pytest.raises(MissingToolHandlerError):
        executor.execute(
            "search_products", "search_products", '{"search_term": "a"}', _context()
        )


# ---------------------------------------------------------------------------
# Dispatch pipeline: success and every failure category
# ---------------------------------------------------------------------------


def test_search_products_success_result_shape():
    inquiry = FakeProductInquiryService(
        ProductInquiryResponse(
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
    )
    result = _execute(
        _executor(inquiry=inquiry),
        _call("search_products", {"search_term": "Aqua"}),
        _context(),
    )
    assert result.ok
    assert result.tool_call_id == CALL_ID
    assert result.tool_name == "search_products"
    assert result.error is None
    assert result.data["products"][0]["sku"] == "AQUA600"
    assert result.data["products"][0]["unit_price"] == "150.00"


def test_create_draft_order_success_result_shape():
    orders = FakeOrderService(draft=_order_detail())
    result = _execute(
        _executor(orders=orders),
        _call(
            "create_draft_order",
            {"items": [{"product_id": str(PRODUCT_ID), "quantity": 10}]},
        ),
        _context(),
    )
    assert result.ok
    assert result.data["order_id"] == str(ORDER_ID)
    assert result.data["status"] == "draft"


def test_confirm_order_success_with_approval():
    orders = FakeOrderService(
        draft=_order_detail(),
        confirmed=_order_detail(status=OrderStatus.CONFIRMED),
    )
    result = _execute(
        _executor(orders=orders),
        _call("confirm_order", {"order_id": str(ORDER_ID)}),
        _context(approved=True),
    )
    assert result.ok
    assert result.data["status"] == "confirmed"
    assert orders.confirm_ids == [ORDER_ID]


def test_unknown_tool_becomes_structured_error():
    result = _execute(_executor(), _call("delete_everything", {}), _context())
    assert not result.ok
    assert result.error.code == "unknown_tool"
    assert result.tool_call_id == CALL_ID
    assert "delete_everything" in result.error.message


def test_malformed_arguments_become_structured_error():
    result = _execute(_executor(), _call("search_products", "{bukan json"), _context())
    assert not result.ok
    assert result.error.code == "malformed_arguments"


def test_invalid_arguments_become_structured_error_with_field_detail():
    result = _execute(
        _executor(),
        _call(
            "create_draft_order",
            {"items": [{"product_id": str(PRODUCT_ID), "quantity": 0}]},
        ),
        _context(),
    )
    assert not result.ok
    assert result.error.code == "invalid_arguments"
    issues = result.error.detail["errors"]
    assert any("quantity" in issue["field"] for issue in issues)


def test_confirmation_without_approval_never_reaches_service():
    orders = FakeOrderService(draft=_order_detail())
    result = _execute(
        _executor(orders=orders),
        _call("confirm_order", {"order_id": str(ORDER_ID)}),
        _context(),
    )
    assert not result.ok
    assert result.error.code == "unauthorized"
    assert orders.get_order_ids == []  # refused before any service call
    assert orders.confirm_ids == []


def test_service_insufficient_stock_maps_to_business_rule():
    orders = FakeOrderService(
        draft=_order_detail(),
        error=InsufficientStockError(PRODUCT_ID, 10, 5),
    )
    result = _execute(
        _executor(orders=orders),
        _call("confirm_order", {"order_id": str(ORDER_ID)}),
        _context(approved=True),
    )
    assert not result.ok
    assert result.error.code == "business_rule"
    assert result.error.detail == {
        "product_id": str(PRODUCT_ID),
        "requested": 10,
        "available": 5,
    }


def test_service_credit_limit_maps_to_business_rule_with_amounts():
    orders = FakeOrderService(
        draft=_order_detail(),
        error=CreditLimitExceededError(
            CUSTOMER_ID, Decimal("10000.00"), Decimal("12000.00")
        ),
    )
    result = _execute(
        _executor(orders=orders),
        _call("confirm_order", {"order_id": str(ORDER_ID)}),
        _context(approved=True),
    )
    assert result.error.code == "business_rule"
    assert result.error.detail["credit_limit"] == "10000.00"
    assert result.error.detail["projected_exposure"] == "12000.00"


def test_service_not_found_maps_to_not_found():
    orders = FakeOrderService(draft=None)
    result = _execute(
        _executor(orders=orders),
        _call("confirm_order", {"order_id": str(ORDER_ID)}),
        _context(approved=True),
    )
    assert result.error.code == "not_found"


def test_unexpected_exception_is_sanitized():
    class FakeBrokenOrders(FakeOrderService):
        def confirm_order(self, order_id):
            raise RuntimeError("psycopg connection exploded: secret stuff")

    orders = FakeBrokenOrders(draft=_order_detail())
    result = _execute(
        _executor(orders=orders),
        _call("confirm_order", {"order_id": str(ORDER_ID)}),
        _context(approved=True),
    )
    assert not result.ok
    assert result.error.code == "execution_error"
    # type name only, the message never leaks internals
    assert result.error.detail == {"exception_type": "RuntimeError"}
    assert "secret" not in result.error.message


def test_every_tool_call_result_carries_call_id_and_name():
    cases = [
        _execute(
            _executor(),
            _call("search_products", {"search_term": "x"}, "call_a"),
            _context(),
        ),
        _execute(_executor(), _call("nope", {}, "call_b"), _context()),
    ]
    assert [(r.tool_call_id, r.tool_name) for r in cases] == [
        ("call_a", "search_products"),
        ("call_b", "nope"),
    ]


def test_results_are_json_serializable_for_the_provider():
    # P2.5 converts results into tool messages; serialization must hold
    orders = FakeOrderService(draft=_order_detail())
    result = _execute(
        _executor(orders=orders),
        _call(
            "create_draft_order",
            {"items": [{"product_id": str(PRODUCT_ID), "quantity": 10}]},
        ),
        _context(),
    )
    assert json.loads(result.model_dump_json(exclude_none=True))["ok"] is True
