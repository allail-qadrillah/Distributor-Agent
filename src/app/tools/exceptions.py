class ToolError(Exception):
    pass


class DuplicateToolError(ToolError):
    def __init__(self, name: str) -> None:
        super().__init__(f"tool already registered: {name!r}")
        self.name = name


class UnknownToolError(ToolError):
    def __init__(self, name: str) -> None:
        super().__init__(f"unknown tool: {name!r}")
        self.name = name


class MissingToolHandlerError(ToolError):
    def __init__(self, name: str) -> None:
        super().__init__(f"tool {name!r} has no registered handler")
        self.name = name


class ToolHandlerError(ToolError):
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
