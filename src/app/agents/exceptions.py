from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agents.schemas import AgentMessage, AgentRunEvent


class AgentError(Exception):
    partial_transcript: "list[AgentMessage] | None" = None
    partial_events: "list[AgentRunEvent] | None" = None


class AgentConfigurationError(AgentError):
    pass


class AgentProviderError(AgentError):
    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        request_id: str | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.detail = detail


class AgentAuthenticationError(AgentProviderError):
    pass


class AgentRateLimitError(AgentProviderError):
    def __init__(self, message: str, *, retry_after: float | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class AgentTimeoutError(AgentProviderError):
    pass


class AgentProviderUnavailableError(AgentProviderError):
    pass


class AgentInvalidResponseError(AgentProviderError):
    pass


class AgentUnsupportedResponseError(AgentProviderError):
    pass


class AgentRoundLimitError(AgentError):
    def __init__(self, max_rounds: int) -> None:
        super().__init__(
            f"agent loop exceeded {max_rounds} tool rounds without a final response"
        )
        self.max_rounds = max_rounds


class AgentToolCallLimitError(AgentError):
    def __init__(self, limit_kind: str, limit: int, requested: int) -> None:
        super().__init__(
            f"tool call limit exceeded ({limit_kind}): requested {requested}, "
            f"limit {limit}"
        )
        self.limit_kind = limit_kind
        self.limit = limit
        self.requested = requested
