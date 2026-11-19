from typing import Literal

from pydantic import model_validator

from app.schemas.common import BaseSchema

ToolResultCategory = Literal[
    "unknown_tool",
    "malformed_arguments",
    "invalid_arguments",
    "unauthorized",
    "not_found",
    "inactive",
    "missing_data",
    "business_rule",
    "execution_error",
]


class ToolResultError(BaseSchema):
    code: ToolResultCategory
    message: str
    detail: dict | None = None


class ToolResult(BaseSchema):
    tool_call_id: str
    tool_name: str
    ok: bool
    data: dict | None = None
    error: ToolResultError | None = None

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> "ToolResult":
        if self.ok and self.error is not None:
            raise ValueError("ok=True must not carry an error")
        if not self.ok and self.data is not None:
            raise ValueError("ok=False must not carry data")
        if not self.ok and self.error is None:
            raise ValueError("ok=False requires an error")
        return self

    @classmethod
    def success(cls, tool_call_id: str, tool_name: str, data: dict) -> "ToolResult":
        return cls(tool_call_id=tool_call_id, tool_name=tool_name, ok=True, data=data)

    @classmethod
    def failure(
        cls,
        tool_call_id: str,
        tool_name: str,
        code: ToolResultCategory,
        message: str,
        detail: dict | None = None,
    ) -> "ToolResult":
        return cls(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            ok=False,
            error=ToolResultError(code=code, message=message, detail=detail),
        )
