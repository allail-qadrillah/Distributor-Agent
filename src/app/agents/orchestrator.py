import logging
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
from app.observability import current_correlation_id, log_event
from app.tools.context import ToolContext
from app.tools.executor import ToolExecutor

_log = logging.getLogger(__name__)

DEFAULT_MAX_TOOL_ROUNDS = 5
DEFAULT_MAX_TOOL_CALLS_PER_ROUND = 8
DEFAULT_MAX_TOOL_CALLS_TOTAL = 16


def _duration_ms(started: float, ended: float) -> int:
    return int((ended - started) * 1000)


def _event_correlation_id(context: ToolContext) -> str:
    # inside a request the trace id wins; outside one (tests, scripts)
    # the conversation id remains the fallback correlation
    return current_correlation_id() or context.conversation_id


class AgentOrchestrator:
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
        messages = list(agent_input.messages)
        tools = self._tool_executor.openai_tools()
        events: list[AgentRunEvent] = []
        executed_calls = 0
        try:
            for round_index in range(self._max_tool_rounds):
                log_event(
                    _log,
                    "provider_request_started",
                    level=logging.DEBUG,
                    round=round_index,
                )
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
                        correlation_id=_event_correlation_id(context),
                        response_id=response.response_id,
                        outcome=response.kind,
                        duration_ms=_duration_ms(started, self._clock()),
                    )
                )
                log_event(
                    _log,
                    "provider_response_received",
                    outcome=response.kind,
                    response_id=response.response_id,
                    duration_ms=_duration_ms(started, self._clock()),
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
                self.refuse_oversized_batch(calls, executed_calls)
                messages.append(AgentMessage(role="assistant", tool_calls=calls))
                for call in calls:
                    log_event(
                        _log,
                        "tool_execution_started",
                        level=logging.DEBUG,
                        tool_name=call.name,
                        tool_call_id=call.id,
                    )
                    started = self._clock()
                    result = self._tool_executor.execute(
                        call.id, call.name, call.arguments, context
                    )
                    events.append(
                        AgentRunEvent(
                            kind="tool_execution",
                            correlation_id=_event_correlation_id(context),
                            tool_call_id=call.id,
                            tool_name=call.name,
                            ok=result.ok,
                            category=result.error.code if result.error else None,
                            duration_ms=_duration_ms(started, self._clock()),
                        )
                    )
                    log_event(
                        _log,
                        "tool_execution_completed",
                        tool_name=call.name,
                        tool_call_id=call.id,
                        ok=result.ok,
                        category=result.error.code if result.error else None,
                        duration_ms=_duration_ms(started, self._clock()),
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
            if events:
                exc.partial_transcript = messages
                exc.partial_events = events
            raise

    def refuse_oversized_batch(
        self, calls: list[AgentToolCall], executed_calls: int
    ) -> None:
        if len(calls) > self._max_tool_calls_per_round:
            raise AgentToolCallLimitError(
                "per_round", self._max_tool_calls_per_round, len(calls)
            )
        if executed_calls + len(calls) > self._max_tool_calls_total:
            raise AgentToolCallLimitError(
                "total", self._max_tool_calls_total, executed_calls + len(calls)
            )
