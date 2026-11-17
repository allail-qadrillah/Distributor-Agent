"""Agent-layer domain exceptions.

Mirrors app.services.exceptions: the provider adapter raises these for
configuration and communication failures; the API layer (future) maps
them to user-safe responses. Every provider failure carries the
provider's error code and request_id for logging, never the API key.
"""

from typing import Any


class AgentError(Exception):
    """Base for all agent-layer failures."""


class AgentConfigurationError(AgentError):
    """Missing or invalid agent configuration (API key, model, base URL)."""


class AgentProviderError(AgentError):
    """Base for provider communication failures.

    code is the provider's stable error code (branch on it, not on the
    message); request_id identifies the provider-side request; detail
    keeps structured context for logging.
    """

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
    """Provider rejected the credential (HTTP 401)."""


class AgentRateLimitError(AgentProviderError):
    """Provider rate-limited the request (HTTP 429)."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class AgentTimeoutError(AgentProviderError):
    """The provider did not answer within the client timeout."""


class AgentProviderUnavailableError(AgentProviderError):
    """Provider unreachable, all sellers unhealthy, or billing blocked."""


class AgentInvalidResponseError(AgentProviderError):
    """Provider response could not be parsed into a valid completion."""


class AgentUnsupportedResponseError(AgentProviderError):
    """Valid completion, but a shape this milestone does not support
    (e.g. multimodal content blocks or non-function tool types)."""


class AgentRoundLimitError(AgentError):
    """The multi-turn loop hit its round limit without a final text
    response. Raised (not silently answered) so the caller decides how
    to apologize to the customer; max_rounds is the configured bound."""

    def __init__(self, max_rounds: int) -> None:
        super().__init__(
            f"agent loop exceeded {max_rounds} tool rounds without a final response"
        )
        self.max_rounds = max_rounds


class AgentToolCallLimitError(AgentError):
    """A model response asked for more tool calls than the execution
    limits allow (per round or per whole run). Checked BEFORE any call
    in the round executes, a batch is never partially run. Raised so
    the caller decides how to respond; the model never sees a fake
    partial success."""

    def __init__(self, limit_kind: str, limit: int, requested: int) -> None:
        super().__init__(
            f"tool call limit exceeded ({limit_kind}): requested {requested}, "
            f"limit {limit}"
        )
        self.limit_kind = limit_kind
        self.limit = limit
        self.requested = requested
