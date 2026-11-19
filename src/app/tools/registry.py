from collections.abc import Callable

from app.tools.definitions import (
    CONFIRM_ORDER_TOOL,
    CREATE_DRAFT_ORDER_TOOL,
    SEARCH_PRODUCTS_TOOL,
    ToolDefinition,
)
from app.tools.exceptions import (
    DuplicateToolError,
    MissingToolHandlerError,
    UnknownToolError,
)

ToolHandler = Callable[..., dict]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(
        self, tool: ToolDefinition, handler: ToolHandler | None = None
    ) -> None:
        if tool.name in self._tools:
            raise DuplicateToolError(tool.name)
        self._tools[tool.name] = tool
        if handler is not None:
            self._handlers[tool.name] = handler

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(name) from None

    def handler(self, name: str) -> ToolHandler:
        if name not in self._tools:
            raise UnknownToolError(name)
        try:
            return self._handlers[name]
        except KeyError:
            raise MissingToolHandlerError(name) from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_tools(self) -> list[dict]:
        return [tool.to_openai_spec() for tool in self._tools.values()]


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(SEARCH_PRODUCTS_TOOL)
    registry.register(CREATE_DRAFT_ORDER_TOOL)
    registry.register(CONFIRM_ORDER_TOOL)
    return registry
