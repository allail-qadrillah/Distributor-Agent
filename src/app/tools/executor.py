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
    def __init__(self, services: ToolServices, registry: ToolRegistry) -> None:
        self._services = services
        self._registry = registry

    def execute(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: str,
        context: ToolContext,
    ) -> ToolResult:
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
            return ToolResult.failure(
                tool_call_id,
                tool_name,
                "execution_error",
                "Kesalahan internal saat menjalankan tool.",
                {"exception_type": type(exc).__name__},
            )
        return ToolResult.success(tool_call_id, tool_name, data)

    def openai_tools(self) -> list[dict]:
        return self._registry.openai_tools()

    def _unknown(self, tool_call_id: str, tool_name: str) -> ToolResult:
        return ToolResult.failure(
            tool_call_id,
            tool_name,
            "unknown_tool",
            f"Tool {tool_name!r} tidak dikenal.",
        )


def build_tool_executor(services: ToolServices) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(SEARCH_PRODUCTS_TOOL, execute_search_products)
    registry.register(CREATE_DRAFT_ORDER_TOOL, execute_create_draft_order)
    registry.register(CONFIRM_ORDER_TOOL, execute_confirm_order)
    return ToolExecutor(services, registry)
