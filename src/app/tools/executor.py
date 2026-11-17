"""Tool executor, the single entry point for running one tool call.

Flow:
    model tool call → registry → argument validation → handler →
    business service → structured ToolResult.

Layering: this module sits BELOW the agent layer (orchestrator →
tools → services) and takes tool calls as primitives (id, name, raw
JSON arguments) so the tools package never imports agent code.

Every model-reachable failure becomes a ToolResult, never an exception
past this layer: unknown tool, malformed JSON, schema violations,
authorization refusals, mapped service business errors, and bounded
execution errors. The model receives the outcome as data it can read
and relay, it can never see success that did not happen. Wiring errors
(MissingToolHandlerError: a registered tool with no handler) are
programming mistakes, not model failures: they raise loudly so the
broken wiring is fixed, never silently degraded.
"""

from app.services.exceptions import ServiceError
from app.tools.arguments import (
    InvalidToolArgumentsError,
    MalformedToolArgumentsError,
    parse_tool_arguments,
)
from app.tools.context import ToolContext
from app.tools.definitions import (
    CONFIRM_ORDER_TOOL,
    CREATE_DRAFT_ORDER_TOOL,
    SEARCH_PRODUCTS_TOOL,
)
from app.tools.exceptions import ToolHandlerError, UnknownToolError
from app.tools.handlers import (
    ToolServices,
    execute_confirm_order,
    execute_create_draft_order,
    execute_search_products,
    map_service_error,
)
from app.tools.registry import ToolRegistry
from app.tools.results import ToolResult


class ToolExecutor:
    """Executes model-requested tool calls against registered tools."""

    def __init__(self, services: ToolServices, registry: ToolRegistry) -> None:
        """registry is explicit: a handlerless default would build an
        executor whose every execute() explodes at lookup, so the caller
        names the wiring (build_tool_executor does it for the app)."""
        self._services = services
        self._registry = registry

    def execute(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: str,
        context: ToolContext,
    ) -> ToolResult:
        """Run one model-requested tool call to a structured outcome."""
        if not self._registry.has(tool_name):
            return self._unknown(tool_call_id, tool_name)
        try:
            args = parse_tool_arguments(tool_name, arguments)
        except UnknownToolError:
            return self._unknown(tool_call_id, tool_name)
        except MalformedToolArgumentsError:
            return ToolResult.failure(
                tool_call_id,
                tool_name,
                "malformed_arguments",
                "Argumen tool bukan objek JSON yang valid.",
            )
        except InvalidToolArgumentsError as exc:
            return ToolResult.failure(
                tool_call_id,
                tool_name,
                "invalid_arguments",
                "Argumen tool tidak lolos validasi.",
                {"errors": [{"field": loc, "issue": msg} for loc, msg in exc.errors]},
            )
        handler = self._registry.handler(tool_name)
        try:
            data = handler(args, context, self._services)
        except ToolHandlerError as exc:
            return ToolResult.failure(
                tool_call_id, tool_name, exc.code, exc.message, exc.detail
            )
        except ServiceError as exc:
            mapped = map_service_error(exc)
            return ToolResult.failure(
                tool_call_id, tool_name, mapped.code, mapped.message, mapped.detail
            )
        except Exception as exc:
            # unexpected (e.g. psycopg) failures: bounded and sanitized.
            # The model sees a generic failure, logs keep the type name
            return ToolResult.failure(
                tool_call_id,
                tool_name,
                "execution_error",
                "Kesalahan internal saat menjalankan tool.",
                {"exception_type": type(exc).__name__},
            )
        return ToolResult.success(tool_call_id, tool_name, data)

    def openai_tools(self) -> list[dict]:
        """The tool specs this executor can run, for the provider call."""
        return self._registry.openai_tools()

    def _unknown(self, tool_call_id: str, tool_name: str) -> ToolResult:
        return ToolResult.failure(
            tool_call_id,
            tool_name,
            "unknown_tool",
            f"Tool {tool_name!r} tidak dikenal.",
        )


def build_tool_executor(services: ToolServices) -> ToolExecutor:
    """Phase 2 default wiring: all three tools with definitions AND
    handlers, every tool exposed to the model is executable here."""
    registry = ToolRegistry()
    registry.register(SEARCH_PRODUCTS_TOOL, execute_search_products)
    registry.register(CREATE_DRAFT_ORDER_TOOL, execute_create_draft_order)
    registry.register(CONFIRM_ORDER_TOOL, execute_confirm_order)
    return ToolExecutor(services, registry)
