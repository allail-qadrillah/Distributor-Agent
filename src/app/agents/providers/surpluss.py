"""Surpluss Intelligence provider adapter, GLM via OpenAI-compatible
chat completions.

Verified provider contract (docs: surplusintelligence.ai llms.txt,
/api-reference/chat-completions, checked live 2026-11-17):
- POST {base_url}/chat/completions, Authorization: Bearer <inf_... key>
- OpenAI request/response envelope; tools and tool_choice in OpenAI
  format; tool calls arrive as choices[0].message.tool_calls[]
  (id, type "function", function.name, function.arguments JSON string)
- Errors: {"error": {type, code, message, ...}, "request_id": ...}.
  Branch on error.code (stable), never on message
- CRITICAL documented trap: a long-running NON-STREAMING request always
  returns HTTP 200, even on failure, with the error envelope in the body.
  Success must be decided by the body (no error + choices present), not
  by the status line. This is why the integration uses plain httpx
  instead of the OpenAI SDK: the SDK's status-based success check treats
  those failed 200s as completions.

This module is the only file that knows the provider wire format. It
receives internal AgentMessage objects and returns internal AgentResponse
objects; business logic, instructions, and orchestration live elsewhere.
No repositories, no SQL, no transactions here.
"""

import math
import time
from collections.abc import Callable

import httpx
from pydantic import ValidationError

from app.agents.exceptions import (
    AgentAuthenticationError,
    AgentConfigurationError,
    AgentInvalidResponseError,
    AgentProviderError,
    AgentProviderUnavailableError,
    AgentRateLimitError,
    AgentTimeoutError,
    AgentUnsupportedResponseError,
)
from app.agents.schemas import AgentMessage, AgentResponse, AgentToolCall

_COMPLETIONS_PATH = "/chat/completions"
# generous read timeout: the provider documents legitimately long-running
# (non-streaming) generations; tight connect timeout fails fast on
# unreachable routing
_DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_MAX_ERROR_SNIPPET = 200  # logged body prefix when the body is unparseable

_AUTH_ERROR_CODES = {"invalid_api_key", "unauthorized", "authentication_error"}

# Transient-only retry (P2.6). A retried completion request has NO
# business side effects (the model has not executed any tool yet), which
# is why retry lives here and tool executions are never retried.
DEFAULT_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 0.5  # seconds, exponential per attempt: 0.5, 1.0, ...
_MAX_RETRY_WAIT = 8.0  # never sleep longer than this, even if asked


class SurplussProvider:
    """Sends chat completion requests to Surpluss Intelligence and
    normalizes responses into internal AgentResponse objects.

    Fail-fast configuration: constructing with a missing API key, model,
    or base URL raises AgentConfigurationError, so misconfiguration is
    caught before the first request, not during a customer conversation.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_retry_wait: float = _MAX_RETRY_WAIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise AgentConfigurationError("OPENAI_API_KEY is not configured")
        if not model:
            raise AgentConfigurationError("OPENAI_MODEL is not configured")
        if not base_url:
            raise AgentConfigurationError("OPENAI_BASE_URL is not configured")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport  # test seam: httpx.MockTransport
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait
        self._sleep = sleep  # test seam: injected sleeper, no real waiting

    def complete(
        self,
        messages: list[AgentMessage],
        tools: list[dict] | None = None,
    ) -> AgentResponse:
        """Send one non-streaming chat completion; return the normalized
        response or raise an AgentProviderError subclass.

        messages may contain the full multi-turn transcript: user/assistant
        text turns, assistant tool-call turns, and tool result turns,
        serialized to the provider's documented protocol. tools is the
        OpenAI-format function tool list (from ToolRegistry.openai_tools());
        None sends no tools. tool_choice stays at the provider default
        (auto), the application never forces a call.

        Transient failures are retried a bounded number of times:
        rate limiting (honoring Retry-After when it fits the wait cap)
        and provider unavailability (transport errors, 5xx). Timeouts
        are NOT retried, the read timeout (120s) has already elapsed
        and a retry would double the customer's wait. Authentication,
        configuration, and response-shape failures are permanent: they
        raise on the first attempt.
        """
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [self._wire_message(m) for m in messages],
        }
        if tools is not None:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {self._api_key}"}
        attempt = 0
        while True:
            try:
                response = self._request(payload, headers)
                return self._normalize(response)
            except AgentProviderUnavailableError:
                # transport errors and 5xx/503 routing: retryable
                wait = self._retry_wait(attempt, None)
                if wait is None:
                    raise
            except AgentRateLimitError as exc:
                # honor Retry-After when it fits the wait cap
                wait = self._retry_wait(attempt, exc.retry_after)
                if wait is None:
                    raise
            self._sleep(wait)
            attempt += 1
            continue

    def _request(self, payload: dict, headers: dict) -> httpx.Response:
        """One HTTP round trip; transport/timeout errors mapped here."""
        try:
            with httpx.Client(
                base_url=self._base_url,
                headers=headers,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                return client.post(_COMPLETIONS_PATH, json=payload)
        except httpx.TimeoutException as exc:
            raise AgentTimeoutError(
                f"provider timed out after {self._timeout.read}s"
            ) from exc
        except httpx.TransportError as exc:
            raise AgentProviderUnavailableError(f"provider unreachable: {exc}") from exc

    def _backoff_delay(self, attempt: int) -> float:
        return _RETRY_BASE_DELAY * (2**attempt)

    def _retry_wait(self, attempt: int, suggested: float | None) -> float | None:
        """Seconds to wait before the next attempt, None when the retry
        budget is spent or the suggested wait exceeds the cap (raise)."""
        if attempt >= self._max_retries:
            return None
        wait = suggested if suggested is not None else self._backoff_delay(attempt)
        if wait > self._max_retry_wait:
            return None
        return wait

    def _wire_message(self, message: AgentMessage) -> dict:
        """One internal AgentMessage → provider wire object.

        Tool-call turns and tool results follow the provider's documented
        multi-turn transcript format: an assistant message with null
        content plus tool_calls, then role:'tool' messages whose
        tool_call_id matches the call they answer.
        """
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        if message.tool_calls:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in message.tool_calls
                ],
            }
        return {"role": message.role, "content": message.content}

    def _normalize(self, response: httpx.Response) -> AgentResponse:
        try:
            body = response.json()
        except ValueError as exc:
            snippet = response.text[:_MAX_ERROR_SNIPPET]
            raise AgentInvalidResponseError(
                f"provider returned a non-JSON body (HTTP {response.status_code}): {snippet!r}"
            ) from exc
        if not isinstance(body, dict):
            raise AgentInvalidResponseError(
                f"provider returned a non-object JSON body (HTTP {response.status_code})"
            )
        # error-first, regardless of status: long-running failures commit
        # HTTP 200 before the outcome exists (documented provider behavior)
        error = body.get("error")
        if error is not None or "choices" not in body:
            self._raise_provider_error(response, body, error)
        return self._response_from_choices(body)

    def _response_from_choices(self, body: dict) -> AgentResponse:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AgentInvalidResponseError("provider response has no choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise AgentInvalidResponseError("provider choice is not an object")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise AgentInvalidResponseError("provider choice has no message object")

        tool_calls = message.get("tool_calls")
        response_id = body.get("id") if isinstance(body.get("id"), str) else None
        if tool_calls is not None:
            return AgentResponse(
                kind="tool_calls",
                response_id=response_id,
                tool_calls=self._tool_calls(tool_calls),
            )
        content = message.get("content")
        if content is None:
            # empty completion: no text, no tool calls, nothing usable
            raise AgentInvalidResponseError("model message has no content")
        if not isinstance(content, str):
            # multimodal content blocks: parseable but not supported by
            # this milestone, never silently coerced
            raise AgentUnsupportedResponseError(
                f"unsupported message content type: {type(content).__name__}"
            )
        return AgentResponse(kind="text", response_id=response_id, text=content)

    def _tool_calls(self, tool_calls: object) -> list[AgentToolCall]:
        if not isinstance(tool_calls, list) or not tool_calls:
            raise AgentInvalidResponseError("tool_calls must be a non-empty list")
        parsed: list[AgentToolCall] = []
        seen_ids: set[str] = set()
        for entry in tool_calls:
            if not isinstance(entry, dict):
                raise AgentInvalidResponseError("tool call entry is not an object")
            call_type = entry.get("type", "function")
            if call_type != "function":
                raise AgentUnsupportedResponseError(
                    f"unsupported tool call type: {call_type!r}"
                )
            function = entry.get("function")
            if not isinstance(function, dict):
                raise AgentInvalidResponseError("tool call has no function object")
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                raise AgentInvalidResponseError(
                    "tool call arguments must be a raw JSON string"
                )
            try:
                call = AgentToolCall(
                    id=entry["id"], name=function["name"], arguments=arguments
                )
            except KeyError as exc:
                raise AgentInvalidResponseError(
                    f"tool call is missing field {exc}"
                ) from exc
            except ValidationError as exc:
                # provider output is external data: blank or mistyped
                # id/name becomes a domain error, never a raw pydantic
                # exception past this boundary
                raise AgentInvalidResponseError(
                    f"tool call failed validation: {exc.errors()[0]['msg']}"
                ) from exc
            if call.id in seen_ids:
                raise AgentInvalidResponseError(f"duplicate tool call id {call.id!r}")
            seen_ids.add(call.id)
            parsed.append(call)
        return parsed

    def _raise_provider_error(
        self, response: httpx.Response, body: dict, error: object
    ) -> None:
        """Map the provider error envelope to an agent domain exception.

        Branches on error.code (the documented stable key) and falls back
        to the HTTP status. request_id is preserved for logging.
        """
        code: str | None = None
        message = "provider request failed"
        if isinstance(error, dict):
            code = error.get("code") if isinstance(error.get("code"), str) else None
            if isinstance(error.get("message"), str):
                message = error["message"]
        request_id = body.get("request_id")
        request_id = request_id if isinstance(request_id, str) else None
        status = response.status_code
        context = f"HTTP {status}, code {code!r}, request_id {request_id!r}"
        kw = {"code": code, "request_id": request_id, "detail": body.get("detail")}

        if code in _AUTH_ERROR_CODES or status == 401:
            raise AgentAuthenticationError(f"{message} ({context})", **kw)
        if code == "rate_limit_exceeded" or status == 429:
            retry_after: float | None = None
            retry_header = response.headers.get("Retry-After")
            if retry_header is not None:
                try:
                    candidate = float(retry_header)
                except ValueError:
                    pass  # non-numeric Retry-After: keep None
                else:
                    # negative or non-finite hints are invalid values, not
                    # instructions: fall back to the default backoff
                    if math.isfinite(candidate) and candidate >= 0:
                        retry_after = candidate
            raise AgentRateLimitError(
                f"{message} ({context})", retry_after=retry_after, **kw
            )
        if code == "model_not_found":
            # model not in the catalog at all: a configuration problem
            raise AgentConfigurationError(
                f"model {self._model!r} is not in the provider catalog ({context})"
            )
        if (
            code == "no_sellers_for_model"
            or status in (402, 404, 503)
            or (status or 0) >= 500
        ):
            # 402 billing, 404 no sellers, 5xx/503 routing: provider-side
            # conditions the application cannot fix by changing its input
            raise AgentProviderUnavailableError(f"{message} ({context})", **kw)
        raise AgentProviderError(f"{message} ({context})", **kw)
