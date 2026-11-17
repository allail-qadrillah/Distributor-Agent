"""Tool argument validation tests, the model-output trust boundary.

Under test: valid parsing for all three tools, every rejection class
(malformed JSON, wrong type, missing field, extra field, bad UUID,
non-positive quantity), the model-safe error summary, and the sync
between wire schemas (tools.definitions) and Pydantic argument models.
"""

import json
import uuid

import pytest

from app.tools.arguments import (
    ARGUMENT_MODELS,
    ConfirmOrderArgs,
    CreateDraftOrderArgs,
    DraftOrderItemArgs,
    InvalidToolArgumentsError,
    MalformedToolArgumentsError,
    SearchProductsArgs,
    parse_tool_arguments,
)
from app.tools.definitions import (
    CONFIRM_ORDER_TOOL,
    CREATE_DRAFT_ORDER_TOOL,
    SEARCH_PRODUCTS_TOOL,
)
from app.tools.exceptions import UnknownToolError
from app.tools.registry import build_default_registry

PRODUCT_ID = str(uuid.uuid4())
ORDER_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Valid arguments
# ---------------------------------------------------------------------------


def test_search_products_valid():
    args = parse_tool_arguments("search_products", '{"search_term": "Aqua 600 ml"}')
    assert isinstance(args, SearchProductsArgs)
    assert args.search_term == "Aqua 600 ml"


def test_create_draft_order_valid():
    raw = (
        '{"items": [{"product_id": "' + PRODUCT_ID + '", "quantity": 10}],'
        ' "notes": "pesan reguler"}'
    )
    args = parse_tool_arguments("create_draft_order", raw)
    assert isinstance(args, CreateDraftOrderArgs)
    assert args.items[0].product_id == uuid.UUID(PRODUCT_ID)
    assert args.items[0].quantity == 10
    assert args.notes == "pesan reguler"


def test_create_draft_order_notes_optional():
    raw = '{"items": [{"product_id": "' + PRODUCT_ID + '", "quantity": 1}]}'
    args = parse_tool_arguments("create_draft_order", raw)
    assert args.notes is None


def test_confirm_order_valid():
    args = parse_tool_arguments("confirm_order", '{"order_id": "' + ORDER_ID + '"}')
    assert isinstance(args, ConfirmOrderArgs)
    assert args.order_id == uuid.UUID(ORDER_ID)


# ---------------------------------------------------------------------------
# Malformed raw arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "bukan json",
        '{"search_term": "a"',
        '["search_term"]',  # JSON array, not an object
        '"Aqua"',  # JSON string
        "42",  # JSON number
    ],
    ids=["not-json", "truncated", "array", "string", "number"],
)
def test_malformed_arguments_rejected(raw):
    with pytest.raises(MalformedToolArgumentsError) as excinfo:
        parse_tool_arguments("search_products", raw)
    assert excinfo.value.tool_name == "search_products"


# ---------------------------------------------------------------------------
# Schema violations
# ---------------------------------------------------------------------------


def test_missing_required_field_rejected():
    with pytest.raises(InvalidToolArgumentsError) as excinfo:
        parse_tool_arguments("search_products", "{}")
    assert excinfo.value.tool_name == "search_products"
    assert any(loc == "search_term" for loc, _ in excinfo.value.errors)


def test_wrong_type_rejected():
    with pytest.raises(InvalidToolArgumentsError):
        parse_tool_arguments(
            "create_draft_order",
            '{"items": [{"product_id": "' + PRODUCT_ID + '", "quantity": "sepuluh"}]}',
        )


def test_bad_uuid_rejected():
    with pytest.raises(InvalidToolArgumentsError) as excinfo:
        parse_tool_arguments("confirm_order", '{"order_id": "bukan-uuid"}')
    assert any(loc == "order_id" for loc, _ in excinfo.value.errors)


@pytest.mark.parametrize("quantity", [0, -1, 1.5], ids=["zero", "negative", "float"])
def test_non_positive_quantity_rejected(quantity):
    raw = json.dumps({"items": [{"product_id": PRODUCT_ID, "quantity": quantity}]})
    with pytest.raises(InvalidToolArgumentsError):
        parse_tool_arguments("create_draft_order", raw)


def test_quantity_at_int32_maximum_accepted():
    raw = json.dumps({"items": [{"product_id": PRODUCT_ID, "quantity": 2147483647}]})
    args = parse_tool_arguments("create_draft_order", raw)
    assert args.items[0].quantity == 2147483647


def test_quantity_above_int32_maximum_rejected():
    # PostgreSQL INTEGER ceiling: refuse at the boundary, not at insert
    raw = json.dumps({"items": [{"product_id": PRODUCT_ID, "quantity": 2147483648}]})
    with pytest.raises(InvalidToolArgumentsError):
        parse_tool_arguments("create_draft_order", raw)


def test_empty_items_rejected():
    with pytest.raises(InvalidToolArgumentsError) as excinfo:
        parse_tool_arguments("create_draft_order", '{"items": []}')
    assert any(loc == "items" for loc, _ in excinfo.value.errors)


def test_blank_search_term_rejected():
    with pytest.raises(InvalidToolArgumentsError):
        parse_tool_arguments("search_products", '{"search_term": "   "}')


def test_model_cannot_smuggle_customer_identity():
    # the trust boundary: customer_id from model arguments is an unknown
    # field, rejected outright, identity comes from ToolContext only
    with pytest.raises(InvalidToolArgumentsError) as excinfo:
        parse_tool_arguments(
            "create_draft_order",
            '{"items": [{"product_id": "' + PRODUCT_ID + '", "quantity": 1}],'
            ' "customer_id": "' + str(uuid.uuid4()) + '"}',
        )
    assert any(loc == "customer_id" for loc, _ in excinfo.value.errors)


def test_model_cannot_smuggle_price():
    with pytest.raises(InvalidToolArgumentsError):
        parse_tool_arguments(
            "create_draft_order",
            '{"items": [{"product_id": "' + PRODUCT_ID + '", "quantity": 1,'
            ' "unit_price": 1000.0}]}',
        )


def test_model_cannot_smuggle_approval():
    with pytest.raises(InvalidToolArgumentsError):
        parse_tool_arguments(
            "confirm_order",
            '{"order_id": "' + ORDER_ID + '", "approved": true}',
        )


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


def test_unknown_tool_name_rejected_at_boundary():
    with pytest.raises(UnknownToolError):
        parse_tool_arguments("delete_everything", "{}")


# ---------------------------------------------------------------------------
# Error summary stays model-safe
# ---------------------------------------------------------------------------


def test_invalid_arguments_error_summary_is_flat_and_bounded():
    with pytest.raises(InvalidToolArgumentsError) as excinfo:
        parse_tool_arguments("confirm_order", '{"order_id": "nope"}')
    error = excinfo.value
    assert error.tool_name == "confirm_order"
    for loc, msg in error.errors:
        assert isinstance(loc, str)
        assert isinstance(msg, str)
        assert len(msg) < 200


# ---------------------------------------------------------------------------
# Wire schema ↔ Pydantic model sync (closes the dual-representation risk)
# ---------------------------------------------------------------------------


def test_argument_models_cover_exactly_the_wire_schema_fields():
    cases = {
        "search_products": (SearchProductsArgs, False),
        "create_draft_order": (CreateDraftOrderArgs, True),
        "confirm_order": (ConfirmOrderArgs, False),
    }
    tools = {
        "search_products": SEARCH_PRODUCTS_TOOL,
        "create_draft_order": CREATE_DRAFT_ORDER_TOOL,
        "confirm_order": CONFIRM_ORDER_TOOL,
    }
    for name, (model, has_nested_items) in cases.items():
        parameters = tools[name].parameters
        wire_fields = set(parameters["properties"])
        model_fields = set(model.model_fields)
        assert wire_fields == model_fields, name
        if has_nested_items:
            item_wire = set(parameters["properties"]["items"]["items"]["properties"])
            item_model = set(DraftOrderItemArgs.model_fields)
            assert item_wire == item_model, f"{name}.items"
        wire_required = set(parameters.get("required", []))
        model_required = {
            field for field, info in model.model_fields.items() if info.is_required()
        }
        assert wire_required == model_required, name
        assert json.dumps(sorted(wire_fields))  # schemas stay JSON-serializable


def test_argument_models_and_registry_cover_the_same_tools():
    # ARGUMENT_MODELS and the default registry must stay in lockstep: a
    # tool registered without an argument model (or vice versa) breaks
    # parsing or dispatch for the other side
    assert set(ARGUMENT_MODELS) == set(build_default_registry().names())
