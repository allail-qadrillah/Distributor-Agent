"""Tool definition and registry unit tests, pure data, no network.

Under test: the provider wire format of the definitions, the registry
guards (duplicate/unknown), and the safety invariants that must hold for
every default tool: no customer identity argument, no price argument, no
approval argument, and argument shapes that mirror the real service
contracts (OrderCreate / ProductInquiryRequest).
"""

import pytest

from app.tools import (
    CONFIRM_ORDER_TOOL,
    CREATE_DRAFT_ORDER_TOOL,
    SEARCH_PRODUCTS_TOOL,
    ToolDefinition,
    build_default_registry,
)
from app.tools.exceptions import DuplicateToolError, UnknownToolError


def _properties(parameters: dict) -> list[str]:
    return list(parameters.get("properties", {}))


def _walk_keys(schema: dict):
    yield from schema.keys()
    for value in schema.values():
        if isinstance(value, dict):
            yield from _walk_keys(value)
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict):
                    yield from _walk_keys(entry)


# ---------------------------------------------------------------------------
# Definitions follow the verified provider contract
# ---------------------------------------------------------------------------


def test_openai_spec_is_standard_function_format():
    spec = SEARCH_PRODUCTS_TOOL.to_openai_spec()
    assert spec["type"] == "function"
    function = spec["function"]
    assert function["name"] == "search_products"
    assert isinstance(function["description"], str) and function["description"]
    assert function["parameters"] == SEARCH_PRODUCTS_TOOL.parameters


@pytest.mark.parametrize(
    "tool",
    [SEARCH_PRODUCTS_TOOL, CREATE_DRAFT_ORDER_TOOL, CONFIRM_ORDER_TOOL],
    ids=["search_products", "create_draft_order", "confirm_order"],
)
def test_parameters_are_inline_object_schemas(tool: ToolDefinition):
    # hand-written inline schemas: no $ref/$defs for the provider to resolve
    assert tool.parameters["type"] == "object"
    assert "$defs" not in tool.parameters
    assert "$ref" not in tool.parameters
    for key in _walk_keys(tool.parameters):
        assert key != "$ref"


# ---------------------------------------------------------------------------
# Safety invariants (P2.2 acceptance)
# ---------------------------------------------------------------------------


def test_no_tool_accepts_customer_identity():
    for tool in (SEARCH_PRODUCTS_TOOL, CREATE_DRAFT_ORDER_TOOL, CONFIRM_ORDER_TOOL):
        assert "customer_id" not in _walk_keys(tool.parameters), tool.name


def test_no_tool_accepts_prices():
    for tool in (SEARCH_PRODUCTS_TOOL, CREATE_DRAFT_ORDER_TOOL, CONFIRM_ORDER_TOOL):
        for key in _walk_keys(tool.parameters):
            assert "price" not in key.lower(), f"{tool.name}.{key}"


def test_confirm_order_has_only_order_id():
    assert _properties(CONFIRM_ORDER_TOOL.parameters) == ["order_id"]
    assert CONFIRM_ORDER_TOOL.parameters["required"] == ["order_id"]
    # approval lives in trusted application context, never in arguments
    for key in _walk_keys(CONFIRM_ORDER_TOOL.parameters):
        assert "approv" not in key.lower()


def test_search_products_matches_inquiry_request_contract():
    # mirrors ProductInquiryRequest minus the trusted customer_id
    assert _properties(SEARCH_PRODUCTS_TOOL.parameters) == ["search_term"]
    assert SEARCH_PRODUCTS_TOOL.parameters["required"] == ["search_term"]


def test_create_draft_order_matches_order_create_contract():
    # mirrors OrderCreate minus the trusted customer_id; no price fields
    parameters = CREATE_DRAFT_ORDER_TOOL.parameters
    assert set(_properties(parameters)) == {"items", "notes"}
    assert parameters["required"] == ["items"]
    items = parameters["properties"]["items"]
    assert items["type"] == "array"
    assert items["minItems"] == 1
    item_properties = _properties(items["items"])
    assert set(item_properties) == {"product_id", "quantity"}
    assert items["items"]["properties"]["quantity"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 2147483647,
        "description": "Jumlah yang dipesan (unit terkecil).",
    }
    assert set(items["items"]["required"]) == {"product_id", "quantity"}


# ---------------------------------------------------------------------------
# Definition validation (fail at construction, not in a conversation)
# ---------------------------------------------------------------------------


def test_blank_name_rejected():
    with pytest.raises(ValueError, match="name"):
        ToolDefinition(name="  ", description="d", parameters={"type": "object"})


def test_blank_description_rejected():
    with pytest.raises(ValueError, match="description"):
        ToolDefinition(name="t", description=" ", parameters={"type": "object"})


def test_non_object_parameters_rejected():
    with pytest.raises(ValueError, match="object"):
        ToolDefinition(name="t", description="d", parameters={"type": "string"})


def test_non_dict_parameters_rejected():
    with pytest.raises(ValueError, match="dict"):
        ToolDefinition(name="t", description="d", parameters=["nope"])


# ---------------------------------------------------------------------------
# Registry behavior
# ---------------------------------------------------------------------------


def test_default_registry_has_the_three_tools_in_order():
    registry = build_default_registry()
    assert registry.names() == [
        "search_products",
        "create_draft_order",
        "confirm_order",
    ]


def test_registry_get_returns_definition():
    registry = build_default_registry()
    assert registry.get("search_products") is SEARCH_PRODUCTS_TOOL


def test_registry_rejects_duplicate_registration():
    registry = build_default_registry()
    with pytest.raises(DuplicateToolError) as excinfo:
        registry.register(SEARCH_PRODUCTS_TOOL)
    assert excinfo.value.name == "search_products"


def test_registry_rejects_unknown_tool_name():
    registry = build_default_registry()
    assert not registry.has("delete_everything")
    with pytest.raises(UnknownToolError) as excinfo:
        registry.get("delete_everything")
    assert excinfo.value.name == "delete_everything"


def test_openai_tools_emits_every_registered_tool():
    registry = build_default_registry()
    specs = registry.openai_tools()
    assert [spec["function"]["name"] for spec in specs] == [
        "search_products",
        "create_draft_order",
        "confirm_order",
    ]
    for spec in specs:
        assert spec["type"] == "function"


def test_openai_tools_is_rebuilt_per_call():
    registry = build_default_registry()
    first = registry.openai_tools()
    second = registry.openai_tools()
    assert first is not second
    assert first[0] is not second[0]
