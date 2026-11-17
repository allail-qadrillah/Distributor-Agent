"""SurplussProvider live smoke test, NOT run by default.

Manual run (needs real credentials, never prints them):

    RUN_AGENT_SMOKE=1 uv run pytest tests/integration/test_agent_provider_integration.py -v

Requirements:
- .env OPENAI_API_KEY set to a Surplus Intelligence buyer key (inf_...)
- .env OPENAI_MODEL set to a tool-capable model (e.g. glm-5.3)
- OPENAI_BASE_URL default (api.surplusintelligence.ai/v1) reachable

Safety:
- No database access, no repository calls, no writes of any kind.
- Sends one short user message; asserts a valid normalized response.
- The API key never appears in output or assertions.

What is verified live:
- connectivity, auth, and the OpenAI-compatible response envelope
- text normalization with a real model reply
- response_id is the provider completion id
- real tool calls issued by the real model (P2.2 wire format)
- the full orchestrator loop against the real provider (P2.5 protocol:
  tool_calls out, tool results in, final text answer)
"""

import json
import os

import pytest

from app.agents.orchestrator import AgentOrchestrator
from app.agents.providers.surpluss import SurplussProvider
from app.agents.schemas import AgentInput, AgentMessage, AgentResponse
from app.config import settings
from app.tools import build_default_registry
from app.tools.context import ToolContext
from app.tools.results import ToolResult

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_AGENT_SMOKE") != "1",
    reason="live Surplus Intelligence smoke test: set RUN_AGENT_SMOKE=1 to run",
)


@pytest.fixture
def provider() -> SurplussProvider:
    if not settings.openai_api_key:
        pytest.skip("OPENAI_API_KEY not configured: cannot run live agent smoke test")
    if not settings.openai_model:
        pytest.skip("OPENAI_MODEL not configured: cannot run live agent smoke test")
    return SurplussProvider(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        base_url=settings.openai_base_url,
    )


def test_provider_returns_valid_text_response(provider: SurplussProvider):
    result = provider.complete(
        [AgentMessage(role="user", content="Balas dengan tepat satu kata: PONG")]
    )
    assert isinstance(result, AgentResponse)
    assert result.kind == "text"
    assert result.text, "model returned an empty reply"
    assert result.response_id, "provider completion id missing"
    # credential hygiene: the key never leaks into the response object
    assert settings.openai_api_key not in result.text
    assert settings.openai_api_key not in (result.response_id or "")


class CannedToolExecutor:
    """Test double at the tool boundary: returns a plausible success
    result for whatever the real model asks for. No database, no
    writes, the point is the live loop, not the business data."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, str, str]] = []

    def openai_tools(self) -> list[dict]:
        return build_default_registry().openai_tools()

    def execute(self, tool_call_id, tool_name, arguments, context):
        self.executed.append((tool_call_id, tool_name, arguments))
        if tool_name == "search_products":
            data = {
                "products": [
                    {
                        "product_id": "b7d1c2b0-0000-4000-8000-000000000001",
                        "sku": "AQUA600",
                        "name": "Aqua 600 ml",
                        "unit_price": "150.00",
                        "available_stock": 120,
                    }
                ]
            }
        else:
            data = {"note": "smoke test: tool executed"}
        return ToolResult.success(tool_call_id, tool_name, data)


def test_live_model_issues_tool_calls(provider: SurplussProvider):
    result = provider.complete(
        [
            AgentMessage(
                role="user",
                content="Cari produk Aqua menggunakan tool pencarian. "
                "Panggil search_products dengan istilah 'Aqua'.",
            )
        ],
        tools=build_default_registry().openai_tools(),
    )
    assert result.kind == "tool_calls", (
        f"expected the model to call a tool, got kind={result.kind!r}"
    )
    registry = build_default_registry()
    for call in result.tool_calls or []:
        assert registry.has(call.name), f"model called unknown tool {call.name!r}"
        assert isinstance(json.loads(call.arguments), dict), (
            "tool call arguments must be a JSON object"
        )
    assert any(c.name == "search_products" for c in result.tool_calls or [])


def test_live_orchestrator_loop_converges(provider: SurplussProvider):
    executor = CannedToolExecutor()
    agent = AgentOrchestrator(provider, executor)
    context = ToolContext(
        customer_id="b7d1c2b0-0000-4000-8000-000000000002",
        conversation_id="smoke-1",
    )
    result = agent.run(
        AgentInput(
            messages=[AgentMessage(role="user", content="Berapa harga Aqua 600 ml?")]
        ),
        context,
    )
    assert result.final_text, "loop must end with a non-empty answer"
    assert executor.executed, "the model must have executed at least one tool"
    roles = [m.role for m in result.transcript]
    assert "tool" in roles, "the transcript must contain the tool exchange"
    # observability records came back from a live run
    assert result.events
    assert all(m.role != "system" for m in result.transcript)
