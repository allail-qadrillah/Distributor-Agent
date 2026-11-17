"""Agent orchestrator, explicit Python orchestration, no framework.

The multi-turn loop, in one readable flow:

    user message + history
      → provider (with tool definitions)
      → text answer?  → done, return final text + transcript + events
      → tool calls?   → check execution limits (never run partial batches)
                       → execute each via ToolExecutor
                       → append assistant tool-call turn + tool results
                       → provider again (same conversation)
      → repeat until text, the round limit (AgentRoundLimitError),
        or a tool-call limit (AgentToolCallLimitError)

Execution control (P2.6):
- Limits are checked BEFORE a round's calls execute: a model response
  asking for more calls than allowed fails whole, never partially. The
  model must never see a fake partial batch result.
- Tool executions are never retried here: they carry business side
  effects (drafts, confirmations). Transient-ONLY retry lives in the
  provider adapter, where a retry is side-effect free.
- Observability: every provider call and tool execution becomes one
  AgentRunEvent (correlation id, response id, tool name, call id,
  result category, duration), identifying and timing data only, no
  raw arguments, no message content, no credentials. When a run ends
  in an exception, the orchestrator attaches the partial transcript
  and events to the raised AgentError (partial_transcript /
  partial_events): the exception stays the error classification the
  caller logs, the partials let it persist what already happened.

Constraints:
- No SQL, no repositories, no transactions: business truth stays in the
  service layer, reached only through ToolExecutor → handlers.
- No provider wire format here: the orchestrator speaks AgentMessage and
  AgentResponse only. Provider parsing lives in providers/surpluss.py.
- The application owns the conversation: every provider call sends the
  full history (chat completions is stateless); the caller persists
  AgentRunResult.transcript and replays it as the next input history.
- Customer identity never flows through the model: ToolContext is
  injected by the caller and handed straight to the executor.
"""

import time
from collections.abc import Callable

from app.agents.exceptions import (
    AgentError,
    AgentRoundLimitError,
    AgentToolCallLimitError,
)
from app.agents.instructions import AGENT_INSTRUCTIONS
from app.agents.providers.surpluss import SurplussProvider
from app.agents.schemas import (
    AgentInput,
    AgentMessage,
    AgentRunEvent,
    AgentRunResult,
    AgentToolCall,
)
from app.tools.context import ToolContext
from app.tools.executor import ToolExecutor

DEFAULT_MAX_TOOL_ROUNDS = 5
DEFAULT_MAX_TOOL_CALLS_PER_ROUND = 8
DEFAULT_MAX_TOOL_CALLS_TOTAL = 16


def _duration_ms(started: float, ended: float) -> int:
    return int((ended - started) * 1000)


class AgentOrchestrator:
    """Runs the model↔tool loop until a final customer-facing answer."""

    def __init__(
        self,
        provider: SurplussProvider,
        tool_executor: ToolExecutor,
        instructions: str = AGENT_INSTRUCTIONS,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        max_tool_calls_per_round: int = DEFAULT_MAX_TOOL_CALLS_PER_ROUND,
        max_tool_calls_total: int = DEFAULT_MAX_TOOL_CALLS_TOTAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be at least 1")
        if max_tool_calls_per_round < 1:
            raise ValueError("max_tool_calls_per_round must be at least 1")
        if max_tool_calls_total < 1:
            raise ValueError("max_tool_calls_total must be at least 1")
        self._provider = provider
        self._tool_executor = tool_executor
        self._instructions = instructions
        self._max_tool_rounds = max_tool_rounds
        self._max_tool_calls_per_round = max_tool_calls_per_round
        self._max_tool_calls_total = max_tool_calls_total
        self._clock = clock

    def run(self, agent_input: AgentInput, context: ToolContext) -> AgentRunResult:
        """One complete run: loop provider→tools→provider until text.

        Provider failures (auth, rate limit, timeout, ...) raise
        AgentProviderError subclasses unchanged, the caller maps them.
        Non-convergent loops raise AgentRoundLimitError and oversized
        tool batches raise AgentToolCallLimitError, instead of the loop
        inventing an answer. Any AgentError raised after tool exchanges
        carries partial_transcript and partial_events.
        """
        messages = list(agent_input.messages)
        tools = self._tool_executor.openai_tools()
        events: list[AgentRunEvent] = []
        executed_calls = 0
        try:
            for _ in range(self._max_tool_rounds):
                started = self._clock()
                response = self._provider.complete(
                    [
                        AgentMessage(role="system", content=self._instructions),
                        *messages,
                    ],
                    tools=tools,
                )
                events.append(
                    AgentRunEvent(
                        kind="provider_call",
                        correlation_id=context.conversation_id,
                        response_id=response.response_id,
                        outcome=response.kind,
                        duration_ms=_duration_ms(started, self._clock()),
                    )
                )
                if response.kind == "text":
                    final_text = response.text or ""
                    if final_text.strip():
                        messages.append(
                            AgentMessage(role="assistant", content=final_text)
                        )
                    return AgentRunResult(
                        final_text=final_text,
                        response_id=response.response_id,
                        transcript=messages,
                        events=events,
                    )
                calls = response.tool_calls or []
                self._check_call_limits(calls, executed_calls)
                messages.append(AgentMessage(role="assistant", tool_calls=calls))
                for call in calls:
                    started = self._clock()
                    result = self._tool_executor.execute(
                        call.id, call.name, call.arguments, context
                    )
                    events.append(
                        AgentRunEvent(
                            kind="tool_execution",
                            correlation_id=context.conversation_id,
                            tool_call_id=call.id,
                            tool_name=call.name,
                            ok=result.ok,
                            category=result.error.code if result.error else None,
                            duration_ms=_duration_ms(started, self._clock()),
                        )
                    )
                    messages.append(
                        AgentMessage(
                            role="tool",
                            tool_call_id=call.id,
                            content=result.model_dump_json(exclude_none=True),
                        )
                    )
                executed_calls += len(calls)
            raise AgentRoundLimitError(self._max_tool_rounds)
        except AgentError as exc:
            # a failed run may already have committed tool side effects;
            # hand the partial transcript and events to the caller via the
            # exception so it can persist them and not retry those
            # effects blind. Failures before any work stay partial-free.
            if events:
                exc.partial_transcript = messages
                exc.partial_events = events
            raise

    def _check_call_limits(
        self, calls: list[AgentToolCall], executed_calls: int
    ) -> None:
        """Refuse oversized batches before anything executes."""
        if len(calls) > self._max_tool_calls_per_round:
            raise AgentToolCallLimitError(
                "per_round", self._max_tool_calls_per_round, len(calls)
            )
        if executed_calls + len(calls) > self._max_tool_calls_total:
            raise AgentToolCallLimitError(
                "total", self._max_tool_calls_total, executed_calls + len(calls)
            )
