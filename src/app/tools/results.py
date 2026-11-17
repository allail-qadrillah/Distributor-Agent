"""Structured tool results, what tool execution returns to the model.

A ToolResult is ALWAYS JSON-serializable and model-safe: success data
uses plain JSON types (Decimal becomes str to stay exact), errors carry
a stable category code, a Bahasa message the model can relay to the
customer, and optional identifying detail (ids, quantities), never
stack traces, credentials, or internal provider detail.

Categories are the P2.6 observability classification: one code per
failure class, stable strings so logs can group without regexes.
"""

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
    """One tool failure: category code, model-facing message, detail."""

    code: ToolResultCategory
    message: str
    detail: dict | None = None


class ToolResult(BaseSchema):
    """Outcome of one tool call, tied to the provider's tool_call_id so
    the conversation loop (P2.5) can route results back to the model."""

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
