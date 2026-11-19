from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import BaseSchema, NonBlankStr

MessageRole = Literal["system", "user", "assistant", "tool"]


class AgentToolCall(BaseSchema):
    id: NonBlankStr
    name: NonBlankStr
    arguments: str


class AgentMessage(BaseSchema):
    role: MessageRole
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[AgentToolCall] | None = None

    @model_validator(mode="after")
    def _shape_by_role(self) -> "AgentMessage":
        if self.role in ("system", "user"):
            if not (self.content and self.content.strip()):
                raise ValueError(f"{self.role} message content must be non-blank")
            if self.tool_call_id is not None or self.tool_calls is not None:
                raise ValueError(f"{self.role} messages cannot carry tool fields")
        elif self.role == "assistant":
            if self.tool_call_id is not None:
                raise ValueError("assistant messages cannot carry tool_call_id")
            if self.tool_calls is not None:
                if not self.tool_calls:
                    raise ValueError("assistant tool_calls must be a non-empty list")
                if self.content is not None:
                    raise ValueError("assistant tool-call turns carry no text content")
                ids = [call.id for call in self.tool_calls]
                if len(ids) != len(set(ids)):
                    raise ValueError("assistant tool_calls must have unique ids")
            elif not (self.content and self.content.strip()):
                raise ValueError("assistant message needs content or tool_calls")
        else:  # tool
            if self.tool_calls is not None:
                raise ValueError("tool messages cannot carry tool_calls")
            if not (self.tool_call_id and self.tool_call_id.strip()):
                raise ValueError("tool message requires tool_call_id")
            if not (self.content and self.content.strip()):
                raise ValueError("tool message content must be non-blank")
        return self


class AgentInput(BaseSchema):
    messages: list[AgentMessage] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_conversation(self) -> "AgentInput":
        if any(m.role == "system" for m in self.messages):
            raise ValueError(
                "system messages are not accepted in agent input: "
                "the application owns the agent instructions"
            )
        if self.messages[-1].role != "user":
            raise ValueError("the last message must be the current user message")
        self._validate_tool_pairing()
        return self

    def _validate_tool_pairing(self) -> None:
        pending: set[str] = set()
        for message in self.messages:
            if message.role == "assistant" and message.tool_calls is not None:
                if pending:
                    raise ValueError(
                        "tool results must complete a tool-call turn "
                        "before the next turn"
                    )
                pending = {call.id for call in message.tool_calls}
            elif message.role == "tool":
                if not pending:
                    raise ValueError(
                        "tool result has no preceding assistant tool-call turn"
                    )
                if message.tool_call_id not in pending:
                    raise ValueError(
                        f"tool result {message.tool_call_id!r} "
                        "does not match a pending tool call"
                    )
                pending.discard(message.tool_call_id)
            elif pending:
                raise ValueError(
                    "tool results must complete a tool-call turn before the next turn"
                )


class AgentResponse(BaseSchema):
    kind: Literal["text", "tool_calls"]
    response_id: str | None = None
    text: str | None = None
    tool_calls: list[AgentToolCall] | None = None

    @model_validator(mode="after")
    def _require_exactly_one_payload(self) -> "AgentResponse":
        if self.kind == "text":
            if self.text is None:
                raise ValueError("kind='text' requires text")
            if self.tool_calls is not None:
                raise ValueError("kind='text' must not carry tool_calls")
        else:
            if not self.tool_calls:
                raise ValueError("kind='tool_calls' requires a non-empty tool_calls")
            if self.text is not None:
                raise ValueError("kind='tool_calls' must not carry text")
        return self


class AgentRunEvent(BaseSchema):
    kind: Literal["provider_call", "tool_execution"]
    correlation_id: str | None = None
    response_id: str | None = None
    outcome: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    ok: bool | None = None
    category: str | None = None
    duration_ms: int = Field(ge=0)


class AgentRunResult(BaseSchema):
    final_text: str
    response_id: str | None = None
    transcript: list[AgentMessage]
    events: list[AgentRunEvent] = Field(default_factory=list)
