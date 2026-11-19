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

ToolQuantity = Annotated[int, Field(gt=0, le=2_147_483_647)]


class SearchProductsArgs(BaseSchema):
    search_term: NonBlankStr


class DraftOrderItemArgs(BaseSchema):
    product_id: UUID
    quantity: ToolQuantity


class CreateDraftOrderArgs(BaseSchema):
    items: Annotated[list[DraftOrderItemArgs], Field(min_length=1)]
    notes: str | None = None


class ConfirmOrderArgs(BaseSchema):
    order_id: UUID


ARGUMENT_MODELS: dict[str, type[BaseSchema]] = {
    SEARCH_PRODUCTS_TOOL_NAME: SearchProductsArgs,
    CREATE_DRAFT_ORDER_TOOL_NAME: CreateDraftOrderArgs,
    CONFIRM_ORDER_TOOL_NAME: ConfirmOrderArgs,
}


class MalformedToolArgumentsError(ToolError):
    def __init__(self, tool_name: str) -> None:
        super().__init__(f"tool {tool_name!r} arguments must be a valid JSON object")
        self.tool_name = tool_name


class InvalidToolArgumentsError(ToolError):
    def __init__(self, tool_name: str, errors: list[tuple[str, str]]) -> None:
        summary = (
            "; ".join(f"{loc}: {msg}" for loc, msg in errors) or "invalid arguments"
        )
        super().__init__(f"tool {tool_name!r} arguments failed validation: {summary}")
        self.tool_name = tool_name
        self.errors = errors


def parse_tool_arguments(tool_name: str, raw_arguments: str) -> BaseSchema:
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
