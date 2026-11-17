"""Tool argument validation, the trust boundary for model output.

Four concepts stay separate (P2.3):
1. Raw model arguments: AgentToolCall.arguments, a JSON string.
2. Validated arguments: the Pydantic models below.
3. Trusted context: tools.context.ToolContext (application-owned).
4. Internal service requests: built by handlers (P2.4) from 2 + 3.

Validation is strict: JSON must parse, types must match, unknown fields
are rejected (extra="forbid", same convention as app.schemas), UUIDs
must be well-formed, quantities must be positive. Invalid arguments
never reach business services.

The argument models mirror the wire schemas in tools.definitions,
kept in sync by tests (test_tool_arguments.py). Fields are exactly the
model-facing subset: no customer_id (context), no prices (backend),
no approval flags (context).
"""

import json
from typing import Annotated
from uuid import UUID

from pydantic import Field, ValidationError

from app.schemas.common import BaseSchema, NonBlankStr
from app.tools.definitions import (
    CONFIRM_ORDER_TOOL_NAME,
    CREATE_DRAFT_ORDER_TOOL_NAME,
    SEARCH_PRODUCTS_TOOL_NAME,
)
from app.tools.exceptions import ToolError, UnknownToolError

# mirrors the wire schema: integer, minimum 1 (orders CHECK quantity > 0),
# maximum 2_147_483_647 (PostgreSQL INTEGER, rejected at the tool boundary
# instead of failing the insert later)
ToolQuantity = Annotated[int, Field(gt=0, le=2_147_483_647)]


class SearchProductsArgs(BaseSchema):
    """search_products, mirrors ProductInquiryRequest minus customer_id."""

    search_term: NonBlankStr


class DraftOrderItemArgs(BaseSchema):
    """One line item, mirrors OrderItemCreate (product_id + quantity only)."""

    product_id: UUID
    quantity: ToolQuantity


class CreateDraftOrderArgs(BaseSchema):
    """create_draft_order, mirrors OrderCreate minus customer_id.

    Duplicate product_id is NOT revalidated here: the handler (P2.4)
    builds a real OrderCreate, whose own validator enforces it once in
    one place. This model only checks the model-facing shape.
    """

    items: Annotated[list[DraftOrderItemArgs], Field(min_length=1)]
    notes: str | None = None


class ConfirmOrderArgs(BaseSchema):
    """confirm_order, identifies the draft; approval lives in ToolContext."""

    order_id: UUID


ARGUMENT_MODELS: dict[str, type[BaseSchema]] = {
    SEARCH_PRODUCTS_TOOL_NAME: SearchProductsArgs,
    CREATE_DRAFT_ORDER_TOOL_NAME: CreateDraftOrderArgs,
    CONFIRM_ORDER_TOOL_NAME: ConfirmOrderArgs,
}


class MalformedToolArgumentsError(ToolError):
    """Raw arguments are not a parseable JSON object."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"tool {tool_name!r} arguments must be a valid JSON object")
        self.tool_name = tool_name


class InvalidToolArgumentsError(ToolError):
    """JSON parsed but failed Pydantic validation.

    errors is a short, model-safe summary (field, message) pairs, enough
    for the model to correct its next attempt, never internal detail.
    """

    def __init__(self, tool_name: str, errors: list[tuple[str, str]]) -> None:
        summary = (
            "; ".join(f"{loc}: {msg}" for loc, msg in errors) or "invalid arguments"
        )
        super().__init__(f"tool {tool_name!r} arguments failed validation: {summary}")
        self.tool_name = tool_name
        self.errors = errors


def parse_tool_arguments(tool_name: str, raw_arguments: str) -> BaseSchema:
    """Validate one model-generated tool call's arguments.

    Returns the validated argument model instance for tool_name. Raises:
    - UnknownToolError: tool_name has no argument model.
    - MalformedToolArgumentsError: raw_arguments is not a JSON object.
    - InvalidToolArgumentsError: schema violation (typed, missing, extra).
    """
    if tool_name not in ARGUMENT_MODELS:
        raise UnknownToolError(tool_name)
    try:
        data = json.loads(raw_arguments)
    except (json.JSONDecodeError, ValueError) as exc:
        raise MalformedToolArgumentsError(tool_name) from exc
    if not isinstance(data, dict):
        raise MalformedToolArgumentsError(tool_name)
    model = ARGUMENT_MODELS[tool_name]
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = [
            (".".join(str(part) for part in error["loc"]) or "(root)", error["msg"])
            for error in exc.errors()
        ]
        raise InvalidToolArgumentsError(tool_name, errors) from exc
