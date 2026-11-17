"""Order workflow safety through the agent, full lifecycle tests.

The stack under test is REAL except the provider and the database:
AgentOrchestrator → build_tool_executor → real handlers → fake
services. The provider is scripted (responses chosen per scenario), so
these tests verify the wired path: tool arguments validated, trusted
context injected, results routed back to the model, guards enforced,
and the transcript persistable between runs, the exact sequence a
customer conversation takes.

Safety scenarios (per the phase contract):
- full lifecycle: search → draft → explicit approval → confirmation;
- confirmation refused without application-granted approval;
- confirmation refused for a draft owned by another customer;
- confirmation of a fabricated (nonexistent) order id;
- service business failure (insufficient stock) surfaces honestly;
- ambiguous search results never auto-order: the model gets every
  candidate and the draft is never created from an ambiguous match.
"""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.agents.orchestrator import AgentOrchestrator
from app.agents.schemas import AgentInput, AgentMessage, AgentResponse, AgentToolCall
from app.schemas.order import OrderDetailResponse, OrderItemResponse, OrderStatus
from app.schemas.product_inquiry import (
    ProductInquiryItem,
    ProductInquiryRequest,
    ProductInquiryResponse,
)
from app.services.exceptions import InsufficientStockError
from app.tools.context import ToolContext
from app.tools.executor import build_tool_executor
from app.tools.handlers import ToolServices

NOW = datetime.now(UTC)
CUSTOMER_ID = uuid.uuid4()
OTHER_CUSTOMER_ID = uuid.uuid4()
PRODUCT_ID = uuid.uuid4()
OTHER_PRODUCT_ID = uuid.uuid4()
ORDER_ID = uuid4()


# ---------------------------------------------------------------------------
# Fakes: provider script + recording services
# ---------------------------------------------------------------------------


class ScriptedProvider:
    """Returns scripted AgentResponses in order and records the exact
    message list the model saw on every call."""

    def __init__(self, script: list[AgentResponse]) -> None:
        self.script = list(script)
        self.seen: list[list[AgentMessage]] = []

    def complete(self, messages, tools=None):
        self.seen.append(list(messages))
        return self.script.pop(0)


class FakeProductInquiryService:
    def __init__(self, products: list[ProductInquiryItem]) -> None:
        self.products = products
        self.requests: list[ProductInquiryRequest] = []

    def search_products(self, request: ProductInquiryRequest) -> ProductInquiryResponse:
        self.requests.append(request)
        return ProductInquiryResponse(products=self.products)


class FakeOrderService:
    """Records every call; behavior per scenario via constructor."""

    def __init__(
        self,
        draft: OrderDetailResponse | None = None,
        confirmed: OrderDetailResponse | None = None,
        confirm_error: Exception | None = None,
    ) -> None:
        self.draft = draft
        self.confirmed = confirmed
        self.confirm_error = confirm_error
        self.create_requests: list = []
        self.get_order_ids: list = []
        self.confirm_ids: list = []

    def create_draft_order(self, order):
        self.create_requests.append(order)
        return self.draft

    def get_order(self, order_id):
        self.get_order_ids.append(order_id)
        if self.draft is None:
            from app.services.exceptions import OrderNotFoundError

            raise OrderNotFoundError(order_id)
        return self.draft

    def confirm_order(self, order_id):
        self.confirm_ids.append(order_id)
        if self.confirm_error is not None:
            raise self.confirm_error
        return self.confirmed


def _product(product_id=PRODUCT_ID, sku="AQUA600", name="Aqua 600 ml"):
    return ProductInquiryItem(
        product_id=product_id,
        sku=sku,
        name=name,
        unit_price=Decimal("150.00"),
        available_stock=120,
    )


def _order_detail(
    *, status=OrderStatus.DRAFT, customer_id=CUSTOMER_ID, order_id=ORDER_ID
) -> OrderDetailResponse:
    return OrderDetailResponse(
        id=order_id,
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
        items=[
            OrderItemResponse(
                id=uuid.uuid4(),
                order_id=order_id,
                product_id=PRODUCT_ID,
                quantity=10,
                unit_price=Decimal("150.00"),
                subtotal=Decimal("1500.00"),
                created_at=NOW,
            )
        ],
    )


def _search_call(call_id="call_s", term="Aqua"):
    return AgentResponse(
        kind="tool_calls",
        response_id="resp-s",
        tool_calls=[
            AgentToolCall(
                id=call_id,
                name="search_products",
                arguments=json.dumps({"search_term": term}),
            )
        ],
    )


def _draft_call(call_id="call_d", quantity=10):
    return AgentResponse(
        kind="tool_calls",
        response_id="resp-d",
        tool_calls=[
            AgentToolCall(
                id=call_id,
                name="create_draft_order",
                arguments=json.dumps(
                    {"items": [{"product_id": str(PRODUCT_ID), "quantity": quantity}]}
                ),
            )
        ],
    )


def _confirm_call(call_id="call_c", order_id=ORDER_ID):
    return AgentResponse(
        kind="tool_calls",
        response_id="resp-c",
        tool_calls=[
            AgentToolCall(
                id=call_id,
                name="confirm_order",
                arguments=json.dumps({"order_id": str(order_id)}),
            )
        ],
    )


def _text(text, response_id="resp-t"):
    return AgentResponse(kind="text", text=text, response_id=response_id)


def _agent(inquiry, orders, script) -> tuple[AgentOrchestrator, ScriptedProvider]:
    provider = ScriptedProvider(script)
    executor = build_tool_executor(ToolServices(product_inquiry=inquiry, orders=orders))
    return AgentOrchestrator(provider, executor), provider


def _context(approved: bool = False) -> ToolContext:
    context = ToolContext(customer_id=CUSTOMER_ID, conversation_id="wa-42")
    return context.with_confirm_order_approval() if approved else context


def _user(text: str) -> AgentMessage:
    return AgentMessage(role="user", content=text)


def _tool_message_provider_saw(provider: ScriptedProvider, call_index: int) -> dict:
    """The tool-result message content from the provider's call_index'th
    request (0-based, AFTER the first)."""
    messages = provider.seen[call_index]
    tool_messages = [m for m in messages if m.role == "tool"]
    return json.loads(tool_messages[-1].content)


# ---------------------------------------------------------------------------
# Full happy lifecycle: search → draft → approval → confirmation
# ---------------------------------------------------------------------------


def test_full_order_lifecycle_across_three_runs():
    inquiry = FakeProductInquiryService([_product()])
    orders = FakeOrderService(
        draft=_order_detail(),
        confirmed=_order_detail(status=OrderStatus.CONFIRMED),
    )
    agent, provider = _agent(
        inquiry,
        orders,
        script=[
            _search_call(),  # run 1: price question
            _text("Aqua 600ml Rp150, stok 120."),
            _draft_call(),  # run 2: order request
            _text("Draft ORD-00000042 tersimpan, total Rp1500. Konfirmasi?"),
            _confirm_call(),  # run 3: explicit user approval
            _text("Pesanan ORD-00000042 terkonfirmasi."),
        ],
    )

    run1 = agent.run(_input_history(_user("berapa harga aqua?")), _context())
    run2 = agent.run(_input_history(*run1.transcript, _user("pesan 10 ya")), _context())
    run3 = agent.run(
        _input_history(*run2.transcript, _user("ya, konfirmasi pesanannya")),
        _context(approved=True),
    )

    # search used the trusted identity, never model arguments
    assert inquiry.requests[0].customer_id == CUSTOMER_ID
    assert inquiry.requests[0].search_term == "Aqua"
    # draft built from context identity + validated args
    created = orders.create_requests[0]
    assert created.customer_id == CUSTOMER_ID
    assert created.items[0].product_id == PRODUCT_ID
    assert created.items[0].quantity == 10
    # confirmation ran exactly once, after ownership check
    assert orders.get_order_ids == [ORDER_ID]
    assert orders.confirm_ids == [ORDER_ID]

    # the model saw the confirmed result before answering
    confirm_result = _tool_message_provider_saw(provider, 5)
    assert confirm_result["ok"] is True
    assert confirm_result["data"]["status"] == "confirmed"

    # final transcript: 3 user turns, 3 tool exchanges, replayable
    roles = [m.role for m in run3.transcript]
    assert roles == ["user", "assistant", "tool"] * 3
    assert run3.final_text == "Pesanan ORD-00000042 terkonfirmasi."

    # observability: one successful confirm event with no error category
    confirm_events = [
        e
        for e in run3.events
        if e.kind == "tool_execution" and e.tool_name == "confirm_order"
    ]
    assert len(confirm_events) == 1
    assert confirm_events[0].ok is True
    assert confirm_events[0].correlation_id == "wa-42"


def _input_history(*messages: AgentMessage) -> AgentInput:
    return AgentInput(messages=list(messages))


# ---------------------------------------------------------------------------
# Guard: no approval, no confirmation
# ---------------------------------------------------------------------------


def test_confirmation_without_application_approval_never_reaches_service():
    orders = FakeOrderService(
        draft=_order_detail(),
        confirmed=_order_detail(status=OrderStatus.CONFIRMED),
    )
    agent, provider = _agent(
        FakeProductInquiryService([_product()]),
        orders,
        script=[
            _confirm_call(),  # model jumps straight to confirmation
            _text("Mohon persetujuan Anda dulu sebelum konfirmasi."),
        ],
    )
    result = agent.run(
        _input_history(_user("konfirmasi pesanannya")),
        _context(),  # NOT approved
    )

    # refused before any service call: ownership read and confirm absent
    assert orders.get_order_ids == []
    assert orders.confirm_ids == []

    refusal = _tool_message_provider_saw(provider, 1)
    assert refusal["ok"] is False
    assert refusal["error"]["code"] == "unauthorized"
    assert "persetujuan" in refusal["error"]["message"]

    # the model still answered the customer honestly
    assert result.final_text == "Mohon persetujuan Anda dulu sebelum konfirmasi."


# ---------------------------------------------------------------------------
# Guard: draft owned by another customer
# ---------------------------------------------------------------------------


def test_confirmation_of_foreign_draft_is_refused():
    orders = FakeOrderService(
        draft=_order_detail(customer_id=OTHER_CUSTOMER_ID),
        confirmed=_order_detail(status=OrderStatus.CONFIRMED),
    )
    agent, provider = _agent(
        FakeProductInquiryService([_product()]),
        orders,
        script=[_confirm_call(), _text("Pesanan tidak dapat diproses.")],
    )
    agent.run(_input_history(_user("konfirmasi")), _context(approved=True))

    assert orders.get_order_ids == [ORDER_ID]  # ownership WAS checked
    assert orders.confirm_ids == []  # and confirmation never ran

    refusal = _tool_message_provider_saw(provider, 1)
    assert refusal["error"]["code"] == "unauthorized"
    assert refusal["error"]["detail"] == {"order_id": str(ORDER_ID)}


# ---------------------------------------------------------------------------
# Guard: fabricated order id
# ---------------------------------------------------------------------------


def test_confirmation_of_fabricated_order_id_is_not_found():
    fabricated = uuid.uuid4()
    orders = FakeOrderService(draft=None)  # no such order at all
    agent, provider = _agent(
        FakeProductInquiryService([_product()]),
        orders,
        script=[
            _confirm_call(order_id=fabricated),
            _text("Pesanan tidak ditemukan."),
        ],
    )
    agent.run(_input_history(_user("konfirmasi pesanan xyz")), _context(approved=True))

    assert orders.get_order_ids == [fabricated]
    assert orders.confirm_ids == []

    result = _tool_message_provider_saw(provider, 1)
    assert result["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Business failure surfaces honestly
# ---------------------------------------------------------------------------


def test_insufficient_stock_failure_is_fed_back_and_confirmed_once():
    orders = FakeOrderService(
        draft=_order_detail(),
        confirm_error=InsufficientStockError(PRODUCT_ID, 10, 5),
    )
    agent, provider = _agent(
        FakeProductInquiryService([_product()]),
        orders,
        script=[
            _confirm_call(),
            _text("Maaf, stok tidak mencukupi untuk pesanan ini."),
        ],
    )
    result = agent.run(_input_history(_user("ya, konfirmasi")), _context(approved=True))

    assert orders.confirm_ids == [ORDER_ID]  # the service was asked once
    failure = _tool_message_provider_saw(provider, 1)
    assert failure["ok"] is False
    assert failure["error"]["code"] == "business_rule"
    assert failure["error"]["detail"] == {
        "product_id": str(PRODUCT_ID),
        "requested": 10,
        "available": 5,
    }
    # the agent apologized instead of claiming success
    assert result.final_text == "Maaf, stok tidak mencukupi untuk pesanan ini."


# ---------------------------------------------------------------------------
# Ambiguity: multiple candidates are handed to the model, nothing ordered
# ---------------------------------------------------------------------------


def test_ambiguous_search_returns_all_candidates_and_never_orders():
    inquiry = FakeProductInquiryService(
        [
            _product(sku="AQUA600", name="Aqua 600 ml"),
            _product(product_id=OTHER_PRODUCT_ID, sku="AQUA1500", name="Aqua 1500 ml"),
        ]
    )
    orders = FakeOrderService(draft=_order_detail())
    agent, provider = _agent(
        inquiry,
        orders,
        script=[
            _search_call(term="Aqua"),
            _text("Ada dua kandidat: Aqua 600 ml atau Aqua 1500 ml?"),
        ],
    )
    result = agent.run(_input_history(_user("harga aqua")), _context())

    # no draft was created from an ambiguous match
    assert orders.create_requests == []

    # the model received BOTH candidates, enough to ask, never to guess
    search_result = _tool_message_provider_saw(provider, 1)
    assert len(search_result["data"]["products"]) == 2
    assert [p["sku"] for p in search_result["data"]["products"]] == [
        "AQUA600",
        "AQUA1500",
    ]

    assert result.final_text == "Ada dua kandidat: Aqua 600 ml atau Aqua 1500 ml?"


# ---------------------------------------------------------------------------
# Model compliance is not trusted, the guards are mechanical
# ---------------------------------------------------------------------------


def test_model_supplied_identity_or_price_is_still_impossible_end_to_end():
    # even if the model hallucinates customer_id/prices into arguments,
    # the boundary rejects them before any service sees them
    inquiry = FakeProductInquiryService([_product()])
    smuggle = AgentResponse(
        kind="tool_calls",
        response_id="resp-x",
        tool_calls=[
            AgentToolCall(
                id="call_x",
                name="create_draft_order",
                arguments=json.dumps(
                    {
                        "items": [{"product_id": str(PRODUCT_ID), "quantity": 10}],
                        "customer_id": str(OTHER_CUSTOMER_ID),
                        "items_prices": [{"unit_price": 1.0}],
                    }
                ),
            )
        ],
    )
    orders = FakeOrderService(draft=_order_detail())
    agent, provider = _agent(
        inquiry, orders, script=[smuggle, _text("Argumen tidak valid.")]
    )
    agent.run(_input_history(_user("pesan 10 untuk toko sebelah")), _context())

    assert orders.create_requests == []  # nothing reached the service
    rejection = _tool_message_provider_saw(provider, 1)
    assert rejection["error"]["code"] == "invalid_arguments"
    fields = {issue["field"] for issue in rejection["error"]["detail"]["errors"]}
    assert "customer_id" in fields  # the smuggled identity is named outright
    assert "items_prices" in fields
