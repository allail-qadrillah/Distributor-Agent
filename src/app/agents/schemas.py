"""Agent input/output schemas, the internal contracts of the agent layer.

These are the ONLY shapes the orchestrator knows. Provider responses are
translated into AgentResponse by the provider adapter, so no provider
wire format leaks past this module's consumers.

Conventions follow app.schemas (Pydantic v2, BaseSchema extra=forbid):
- AgentMessage covers the full multi-turn transcript protocol: user and
  assistant text turns, assistant tool-call turns (content null, tool_calls
  list), and tool result turns (tool_call_id + JSON content). Shape rules
  are enforced per role so an invalid transcript fails here, not at the
  provider.
- AgentInput: application-owned conversation history, newest user message
  last; replaying prior tool exchanges is allowed (the caller persisted
  them from AgentRunResult.transcript). System messages are rejected.
  The orchestrator owns the instructions.
- AgentInput never carries customer identity: authentication context
  stays in the application layer (tools.context.ToolContext) and must
  never come from model output.
- AgentToolCall.arguments stays a raw JSON string: parsing and validation
  happen at the tool-execution boundary (tools.arguments), not here.
- AgentRunResult: one complete orchestration run, the customer-facing
  final text plus the transcript the caller should persist.
"""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import BaseSchema, NonBlankStr

MessageRole = Literal["system", "user", "assistant", "tool"]


class AgentToolCall(BaseSchema):
    """One function/tool call the model wants to make."""

    id: NonBlankStr
    name: NonBlankStr
    arguments: str  # raw JSON object string from the provider


class AgentMessage(BaseSchema):
    """One conversation message, shaped by role:

    - system / user: plain text (non-blank), never tool fields;
    - assistant: either a text answer (content) or a tool-call turn
      (tool_calls, content null), never both, never neither;
    - tool: one tool result (tool_call_id + non-blank JSON content).

    The provider transcript protocol is mirrored exactly, so a valid
    AgentMessage list is always a sendable conversation.
    """

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
    """Application-level agent input: full conversation history, newest
    message last. The caller (future WhatsApp/API layer) owns context.
    There is no persistent conversation storage in this milestone.
    """

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
        return self


class AgentResponse(BaseSchema):
    """Normalized model response: plain text OR tool calls, never both.

    response_id is the provider's completion id when available (useful
    for logging and future continuation support).
    """

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
    """One observability record from a run, pure data, no secrets.

    Fields are deliberately identifying-and-timing only: no raw
    arguments, no message content, no credentials. The caller persists
    or logs them.

    kind="provider_call": one model request. outcome is the response
    kind ("text"/"tool_calls"); category is None.
    kind="tool_execution": one tool call execution. ok/category describe
    the ToolResult; tool_call_id and tool_name identify the call.
    correlation_id is the caller's conversation correlation id
    (ToolContext.conversation_id) when the channel provides one.
    duration_ms is measured by the injected clock.
    """

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
    """One complete orchestration run (P2.5 loop).

    final_text: the customer-facing answer from the model's final
    text turn.
    transcript: the conversation history INCLUDING the assistant
    tool-call turns and tool results of this run, EXCLUDING the system
    instructions, persist this and replay it as the next AgentInput
    history. Without the tool exchanges, the provider would reject the
    next request (tool results must follow their tool calls).
    response_id: the provider completion id of the final response.
    events: per-call observability records (provider calls and tool
    executions) for logging, identifying fields and durations only.
    """

    final_text: str
    response_id: str | None = None
    transcript: list[AgentMessage]
    events: list[AgentRunEvent] = Field(default_factory=list)
