"""Tool registry, maps tool names to their definitions and handlers.

The registry is the single source of truth for which tools exist. Guards:
- Duplicate names raise DuplicateToolError (programming error, fail at
  startup, never silently overwrite).
- Unknown names raise UnknownToolError (model error, surfaced as a
  structured tool result so the model can recover).
- A definition without a handler raises MissingToolHandlerError at
  lookup: a tool is not executable just because its schema exists.

Handler contract (enforced by wiring in tools.executor, not by typing):
handler(validated_args, trusted_context, services) -> dict payload;
failures raise ToolHandlerError or a service exception.

openai_tools() emits the provider wire list for registered tools.
"""

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

# (args, context, services) -> success payload dict; raises on failure
ToolHandler = Callable[..., dict]


class ToolRegistry:
    """Insertion-ordered name → ToolDefinition (+ optional handler) map."""

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
        """The `tools` payload for the provider: one spec per registered
        tool, in registration order. Spec dicts are rebuilt per call; the
        definition objects themselves stay immutable (frozen dataclass)."""
        return [tool.to_openai_spec() for tool in self._tools.values()]


def build_default_registry() -> ToolRegistry:
    """The Phase 2 tool set, definitions only, used where handlers are
    not executable yet (P2.2 shape). Full wiring lives in
    tools.executor.build_tool_executor."""
    registry = ToolRegistry()
    registry.register(SEARCH_PRODUCTS_TOOL)
    registry.register(CREATE_DRAFT_ORDER_TOOL)
    registry.register(CONFIRM_ORDER_TOOL)
    return registry
