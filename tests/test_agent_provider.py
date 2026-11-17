"""SurplussProvider unit tests, mocked HTTP via httpx.MockTransport, no
real network, no API key.

Under test: the verified provider contract (request shape, OpenAI
response normalization, error-envelope mapping, the documented
long-running HTTP-200-with-error-body trap) and the fail-fast
configuration checks.
"""

import json

import httpx
import pytest

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
from app.agents.providers.surpluss import SurplussProvider
from app.agents.schemas import AgentMessage, AgentResponse

API_KEY = "inf_0f8c2a1b4d6e47f9a3c5b7d9e1f24680"
MODEL = "glm-5.3"
BASE_URL = "https://api.surplusintelligence.ai/v1"
USER_MSG = [AgentMessage(role="user", content="Berapa harga Aqua 600ml?")]


def _no_sleep(_: float) -> None:
    """Default sleeper for tests: retry logic runs without real waiting."""


def _provider(handler, **kw) -> tuple[SurplussProvider, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    provider = SurplussProvider(
        api_key=kw.pop("api_key", API_KEY),
        model=kw.pop("model", MODEL),
        base_url=kw.pop("base_url", BASE_URL),
        transport=httpx.MockTransport(record),
        sleep=kw.pop("sleep", _no_sleep),
        **kw,
    )
    return provider, requests


def _text_body(
    text: str = "Halo! Ada yang bisa dibantu?", call_id: str = "chatcmpl-abc123"
) -> dict:
    return {
        "id": call_id,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _tool_body(calls: list[dict]) -> dict:
    return {
        "id": "chatcmpl-tool1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": calls},
            }
        ],
    }


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _error_body(
    code: str, type_: str = "invalid_request_error", message: str = "boom"
) -> dict:
    return {
        "error": {"type": type_, "code": code, "message": message},
        "request_id": "req_1abc",
    }


def _json_response(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


# ---------------------------------------------------------------------------
# Request format
# ---------------------------------------------------------------------------


def test_request_shape_and_auth_header():
    provider, requests = _provider(lambda r: _json_response(_text_body()))
    provider.complete(USER_MSG)
    request = requests[0]
    assert request.url.path.endswith("/chat/completions")
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": "Berapa harga Aqua 600ml?"}]


def test_tools_included_in_payload_when_provided():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_products",
                "description": "cari produk",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    provider, requests = _provider(lambda r: _json_response(_text_body()))
    provider.complete(USER_MSG, tools=tools)
    body = json.loads(requests[0].content)
    assert body["tools"] == tools


def test_tools_absent_from_payload_when_not_provided():
    provider, requests = _provider(lambda r: _json_response(_text_body()))
    provider.complete(USER_MSG)
    assert "tools" not in json.loads(requests[0].content)


def test_tool_transcript_round_trips_in_wire_format():
    from app.agents.schemas import AgentToolCall

    messages = [
        AgentMessage(role="user", content="Cek Aqua"),
        AgentMessage(
            role="assistant",
            tool_calls=[
                AgentToolCall(
                    id="call_1",
                    name="search_products",
                    arguments='{"search_term": "Aqua"}',
                )
            ],
        ),
        AgentMessage(
            role="tool",
            tool_call_id="call_1",
            content='{"tool_call_id": "call_1", "ok": true}',
        ),
    ]
    provider, requests = _provider(lambda r: _json_response(_text_body()))
    provider.complete(messages)
    wire = json.loads(requests[0].content)["messages"]
    assert wire[0] == {"role": "user", "content": "Cek Aqua"}
    assert wire[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "search_products",
                    "arguments": '{"search_term": "Aqua"}',
                },
            }
        ],
    }
    assert wire[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"tool_call_id": "call_1", "ok": true}',
    }


# ---------------------------------------------------------------------------
# Normalization: success paths
# ---------------------------------------------------------------------------


def test_text_response_normalized():
    provider, _ = _provider(lambda r: _json_response(_text_body("Stok tersedia.")))
    result = provider.complete(USER_MSG)
    assert isinstance(result, AgentResponse)
    assert result.kind == "text"
    assert result.text == "Stok tersedia."
    assert result.tool_calls is None
    assert result.response_id == "chatcmpl-abc123"


def test_tool_call_response_normalized():
    provider, _ = _provider(
        lambda r: _json_response(
            _tool_body(
                [_tool_call("call_1", "product_inquiry", '{"search_term": "Aqua"}')]
            )
        )
    )
    result = provider.complete(USER_MSG)
    assert result.kind == "tool_calls"
    assert result.text is None
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "product_inquiry"
    assert call.arguments == '{"search_term": "Aqua"}'


def test_multiple_tool_calls_normalized():
    provider, _ = _provider(
        lambda r: _json_response(
            _tool_body(
                [
                    _tool_call("call_1", "product_inquiry", '{"search_term": "Aqua"}'),
                    _tool_call("call_2", "create_draft_order", '{"items": []}'),
                ]
            )
        )
    )
    result = provider.complete(USER_MSG)
    assert [c.id for c in result.tool_calls] == ["call_1", "call_2"]
    assert [c.name for c in result.tool_calls] == [
        "product_inquiry",
        "create_draft_order",
    ]


# ---------------------------------------------------------------------------
# Normalization: explicit failure paths (never silently coerced)
# ---------------------------------------------------------------------------


def test_empty_choices_is_invalid_response():
    provider, _ = _provider(lambda r: _json_response({"id": "x", "choices": []}))
    with pytest.raises(AgentInvalidResponseError):
        provider.complete(USER_MSG)


def test_null_content_without_tool_calls_is_invalid_response():
    provider, _ = _provider(
        lambda r: _json_response(
            {
                "id": "x",
                "choices": [{"message": {"role": "assistant", "content": None}}],
            }
        )
    )
    with pytest.raises(AgentInvalidResponseError):
        provider.complete(USER_MSG)


def test_multimodal_content_blocks_unsupported():
    body = _text_body()
    body["choices"][0]["message"]["content"] = [
        {"type": "text", "text": "lihat gambar ini"}
    ]
    provider, _ = _provider(lambda r: _json_response(body))
    with pytest.raises(AgentUnsupportedResponseError):
        provider.complete(USER_MSG)


def test_non_function_tool_type_unsupported():
    provider, _ = _provider(
        lambda r: _json_response(
            _tool_body([{"id": "t1", "type": "computer_use", "function": {}}])
        )
    )
    with pytest.raises(AgentUnsupportedResponseError):
        provider.complete(USER_MSG)


def test_tool_call_missing_arguments_invalid():
    provider, _ = _provider(
        lambda r: _json_response(
            _tool_body([{"id": "t1", "type": "function", "function": {"name": "f"}}])
        )
    )
    with pytest.raises(AgentInvalidResponseError):
        provider.complete(USER_MSG)


def test_non_json_body_invalid_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error</html>")

    provider, _ = _provider(handler)
    with pytest.raises(AgentInvalidResponseError):
        provider.complete(USER_MSG)


def test_non_object_json_body_invalid_response():
    provider, _ = _provider(lambda r: _json_response([1, 2, 3]))
    with pytest.raises(AgentInvalidResponseError):
        provider.complete(USER_MSG)


def test_tool_calls_not_a_list_invalid():
    body = _tool_body([])
    body["choices"][0]["message"]["tool_calls"] = "oops"
    provider, _ = _provider(lambda r: _json_response(body))
    with pytest.raises(AgentInvalidResponseError):
        provider.complete(USER_MSG)


# ---------------------------------------------------------------------------
# Error mapping (branch on body error code; status is secondary)
# ---------------------------------------------------------------------------


def test_authentication_failure_maps_with_context():
    provider, _ = _provider(
        lambda r: _json_response(
            _error_body("invalid_api_key", "authentication_error"), 401
        )
    )
    with pytest.raises(AgentAuthenticationError) as excinfo:
        provider.complete(USER_MSG)
    assert excinfo.value.code == "invalid_api_key"
    assert excinfo.value.request_id == "req_1abc"


def test_rate_limit_maps_with_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        response = _json_response(_error_body("rate_limit_exceeded"), 429)
        response.headers["Retry-After"] = "7"
        return response

    provider, _ = _provider(handler)
    with pytest.raises(AgentRateLimitError) as excinfo:
        provider.complete(USER_MSG)
    assert excinfo.value.retry_after == 7.0
    assert excinfo.value.code == "rate_limit_exceeded"


def test_provider_unavailable_503():
    provider, _ = _provider(
        lambda r: _json_response(
            _error_body("no_healthy_sellers", "service_unavailable"), 503
        )
    )
    with pytest.raises(AgentProviderUnavailableError) as excinfo:
        provider.complete(USER_MSG)
    assert excinfo.value.code == "no_healthy_sellers"


def test_billing_402_maps_to_unavailable_with_code():
    provider, _ = _provider(
        lambda r: _json_response(_error_body("insufficient_credit"), 402)
    )
    with pytest.raises(AgentProviderUnavailableError) as excinfo:
        provider.complete(USER_MSG)
    assert excinfo.value.code == "insufficient_credit"


def test_model_not_found_is_configuration_error():
    provider, _ = _provider(
        lambda r: _json_response(_error_body("model_not_found"), 404)
    )
    with pytest.raises(AgentConfigurationError):
        provider.complete(USER_MSG)


def test_no_sellers_for_model_is_unavailable():
    provider, _ = _provider(
        lambda r: _json_response(_error_body("no_sellers_for_model"), 404)
    )
    with pytest.raises(AgentProviderUnavailableError):
        provider.complete(USER_MSG)


def test_unknown_error_code_maps_to_generic_provider_error():
    provider, _ = _provider(lambda r: _json_response(_error_body("surprise_code"), 400))
    with pytest.raises(AgentProviderError) as excinfo:
        provider.complete(USER_MSG)
    assert not isinstance(excinfo.value, AgentProviderUnavailableError)
    assert excinfo.value.code == "surprise_code"


def test_long_running_200_with_error_body_is_failure():
    # documented trap: long-running non-streaming requests commit HTTP
    # 200 before the outcome exists; success must be decided by the body
    provider, _ = _provider(
        lambda r: _json_response(
            _error_body("server_error", "service_unavailable"), 200
        )
    )
    with pytest.raises(AgentProviderError):
        provider.complete(USER_MSG)


def test_500_status_without_error_envelope_is_unavailable():
    provider, _ = _provider(lambda r: _json_response({"unexpected": True}, 500))
    with pytest.raises(AgentProviderError):
        provider.complete(USER_MSG)


# ---------------------------------------------------------------------------
# Transport-level failures
# ---------------------------------------------------------------------------


def test_timeout_maps_to_agent_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    provider, _ = _provider(handler)
    with pytest.raises(AgentTimeoutError):
        provider.complete(USER_MSG)


def test_connect_error_maps_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider, _ = _provider(handler)
    with pytest.raises(AgentProviderUnavailableError):
        provider.complete(USER_MSG)


# ---------------------------------------------------------------------------
# Transient-only bounded retry (P2.6)
# ---------------------------------------------------------------------------


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_rate_limit_retries_and_succeeds_honoring_retry_after():
    responses = [
        _rate_limited_response(retry_after="1"),
        _json_response(_text_body()),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    sleep = SleepRecorder()
    provider, requests = _provider(handler, sleep=sleep)
    result = provider.complete(USER_MSG)
    assert result.kind == "text"
    assert len(requests) == 2  # one failure, one retry
    assert sleep.calls == [1.0]  # Retry-After honored exactly


def test_rate_limit_retry_after_above_cap_raises_without_retrying():
    def handler(request: httpx.Request) -> httpx.Response:
        return _rate_limited_response(retry_after="60")

    sleep = SleepRecorder()
    provider, requests = _provider(handler, sleep=sleep)
    with pytest.raises(AgentRateLimitError):
        provider.complete(USER_MSG)
    assert len(requests) == 1  # waiting 60s serves nobody: fail now
    assert sleep.calls == []


def test_rate_limit_retries_exhausted_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return _rate_limited_response()  # no Retry-After header

    sleep = SleepRecorder()
    provider, requests = _provider(handler, max_retries=1, sleep=sleep)
    with pytest.raises(AgentRateLimitError):
        provider.complete(USER_MSG)
    assert len(requests) == 2  # initial + 1 retry
    assert sleep.calls == [0.5]  # exponential backoff, no header to honor


def test_unavailable_5xx_retries_with_backoff_and_succeeds():
    responses = [
        _json_response(_error_body("server_error", "service_unavailable"), 500),
        _json_response(_text_body()),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    sleep = SleepRecorder()
    provider, requests = _provider(handler, sleep=sleep)
    assert provider.complete(USER_MSG).kind == "text"
    assert len(requests) == 2
    assert sleep.calls == [0.5]


def test_transport_error_retries_then_succeeds():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("connection refused")
        return _json_response(_text_body())

    sleep = SleepRecorder()
    provider, requests = _provider(handler, sleep=sleep)
    assert provider.complete(USER_MSG).kind == "text"
    assert sleep.calls == [0.5]


def test_retries_back_off_exponentially():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_error_body("server_error", "service_unavailable"), 500)

    sleep = SleepRecorder()
    provider, requests = _provider(handler, sleep=sleep)
    with pytest.raises(AgentProviderUnavailableError):
        provider.complete(USER_MSG)
    assert len(requests) == 3  # initial + 2 retries
    assert sleep.calls == [0.5, 1.0]


def test_max_retries_zero_disables_retry():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_error_body("server_error", "service_unavailable"), 500)

    sleep = SleepRecorder()
    provider, requests = _provider(handler, max_retries=0, sleep=sleep)
    with pytest.raises(AgentProviderUnavailableError):
        provider.complete(USER_MSG)
    assert len(requests) == 1
    assert sleep.calls == []


def test_timeout_is_not_retried():
    # the read timeout has already burned 120s: retrying doubles the wait
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    sleep = SleepRecorder()
    provider, requests = _provider(handler, sleep=sleep)
    with pytest.raises(AgentTimeoutError):
        provider.complete(USER_MSG)
    assert len(requests) == 1
    assert sleep.calls == []


def test_authentication_error_is_not_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_error_body("invalid_api_key"), 401)

    sleep = SleepRecorder()
    provider, requests = _provider(handler, sleep=sleep)
    with pytest.raises(AgentAuthenticationError):
        provider.complete(USER_MSG)
    assert len(requests) == 1
    assert sleep.calls == []


def _rate_limited_response(retry_after: str | None = None) -> httpx.Response:
    response = _json_response(_error_body("rate_limit_exceeded"), 429)
    if retry_after is not None:
        response.headers["Retry-After"] = retry_after
    return response


# ---------------------------------------------------------------------------
# Configuration and secret hygiene
# ---------------------------------------------------------------------------


def test_missing_api_key_fails_fast():
    with pytest.raises(AgentConfigurationError):
        SurplussProvider(api_key="", model=MODEL, base_url=BASE_URL)


def test_missing_model_fails_fast():
    with pytest.raises(AgentConfigurationError):
        SurplussProvider(api_key=API_KEY, model="", base_url=BASE_URL)


def test_missing_base_url_fails_fast():
    with pytest.raises(AgentConfigurationError):
        SurplussProvider(api_key=API_KEY, model=MODEL, base_url="")


def test_error_messages_never_contain_the_api_key():
    provider, _ = _provider(
        lambda r: _json_response(
            _error_body("invalid_api_key", "authentication_error"), 401
        )
    )
    try:
        provider.complete(USER_MSG)
        raise AssertionError("expected AgentAuthenticationError")
    except AgentAuthenticationError as exc:
        assert API_KEY not in str(exc)
        assert API_KEY not in repr(exc)
