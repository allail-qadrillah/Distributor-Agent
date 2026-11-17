"""Tool-layer domain exceptions.

Raised by the tool registry and (in later milestones) argument validation
and execution. The orchestrator and API layer branch on these to keep
model-generated failures out of business services.
"""


class ToolError(Exception):
    """Base for all tool-layer failures."""


class DuplicateToolError(ToolError):
    """A tool name was registered twice, a programming error, not a
    model error: fail loudly at startup, never silently overwrite."""

    def __init__(self, name: str) -> None:
        super().__init__(f"tool already registered: {name!r}")
        self.name = name


class UnknownToolError(ToolError):
    """The model requested a tool name that is not in the registry.
    Surfaced to the model as a structured tool error so it can recover
    instead of the request failing opaquely."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown tool: {name!r}")
        self.name = name


class MissingToolHandlerError(ToolError):
    """A registered tool definition has no execution handler attached.
    A programming/setup error, never a model error, so fail loudly instead
    of silently skipping the tool."""

    def __init__(self, name: str) -> None:
        super().__init__(f"tool {name!r} has no registered handler")
        self.name = name


class ToolHandlerError(ToolError):
    """A tool-level failure the handler raises on purpose: authorization
    refusals and other conditions the model must see as a structured,
    model-safe message (code + Bahasa message + optional detail)."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
