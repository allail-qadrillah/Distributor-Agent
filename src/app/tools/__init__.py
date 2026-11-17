"""Tool layer: model-visible tool definitions and the registry.

Direction (README): api → tools → services → repositories → database.
This package owns definitions, argument validation (P2.3), and handlers
(P2.4). It never contains SQL and never duplicates service business
rules.
"""

from app.tools.arguments import (
    ConfirmOrderArgs,
    CreateDraftOrderArgs,
    DraftOrderItemArgs,
    InvalidToolArgumentsError,
    MalformedToolArgumentsError,
    SearchProductsArgs,
    parse_tool_arguments,
)
from app.tools.context import ToolContext
from app.tools.definitions import (
    CONFIRM_ORDER_TOOL,
    CREATE_DRAFT_ORDER_TOOL,
    SEARCH_PRODUCTS_TOOL,
    ToolDefinition,
)
from app.tools.exceptions import (
    DuplicateToolError,
    MissingToolHandlerError,
    ToolError,
    ToolHandlerError,
    UnknownToolError,
)
from app.tools.executor import ToolExecutor, build_tool_executor
from app.tools.handlers import (
    ToolServices,
    execute_confirm_order,
    execute_create_draft_order,
    execute_search_products,
    map_service_error,
)
from app.tools.registry import ToolRegistry, build_default_registry
from app.tools.results import ToolResult, ToolResultError

__all__ = [
    "CONFIRM_ORDER_TOOL",
    "CREATE_DRAFT_ORDER_TOOL",
    "SEARCH_PRODUCTS_TOOL",
    "ConfirmOrderArgs",
    "CreateDraftOrderArgs",
    "DraftOrderItemArgs",
    "InvalidToolArgumentsError",
    "MalformedToolArgumentsError",
    "SearchProductsArgs",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolHandlerError",
    "DuplicateToolError",
    "MissingToolHandlerError",
    "UnknownToolError",
    "ToolExecutor",
    "ToolResult",
    "ToolResultError",
    "ToolServices",
    "ToolRegistry",
    "build_default_registry",
    "build_tool_executor",
    "execute_confirm_order",
    "execute_create_draft_order",
    "execute_search_products",
    "map_service_error",
    "parse_tool_arguments",
]
