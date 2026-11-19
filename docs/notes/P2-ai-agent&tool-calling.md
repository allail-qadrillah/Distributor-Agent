# P2: AI Agent & Tool Calling

Date: 2026-11-17 · Status: complete and verified (unit + regression + DB integration + live provider and model smoke).

## What was built

The full AI-agent foundation for the FMCG sales assistant over WhatsApp. The app sends the customer message plus agent instructions to the LLM. The model may request tools. Tools execute against the existing business services. Results go back to the model, and the loop repeats until a final text answer. Sequential milestones: response detection → tool definitions + registry → argument validation + trusted context → tool execution → the multi-round loop → execution control + retry → order-flow verification → live verification.

## Architecture

```
app layer (WhatsApp / API)
        │  AgentInput(messages) + ToolContext(customer_id, ...)
        ▼
AgentOrchestrator ──── system instructions + history ────► SurplussProvider (adapter)
        │                                                    │ HTTP POST /chat/completions
        │                                                    │ (OpenAI-compatible)
        │  AgentResponse(kind="text" | "tool_calls")
        │
        ▼
   ToolExecutor.execute(tool_call_id, name, arguments, context)
        │  registry → parse_tool_arguments → handler → service
        ▼
   ToolResult(ok, data | error) ──► tool message ──► provider again (loop)
```

Strict dependency direction: `api → tools → services → repositories → database`. The orchestrator in `app.agents` is the top caller. `app.tools` never imports `app.agents`.

Separation of responsibilities: the orchestrator is pure Python flow with no SQL, repositories, transactions, or provider parsing. The adapter is the only module that knows the provider wire format. Handlers are pure translators (validated args + context → a service request). Instructions are pure data (`instructions.py`, in Indonesian).

## Components

| File | Responsibility |
|---|---|
| `src/app/agents/schemas.py` | `AgentMessage` (4 roles: system/user/assistant/tool, shape rules per role), `AgentInput`, `AgentResponse` (text XOR tool_calls), `AgentRunResult` (final_text + response_id + transcript + events), `AgentRunEvent`. |
| `src/app/agents/orchestrator.py` | `AgentOrchestrator.run(input, context)`: the provider→executor→provider loop until the final text, round limit 5, per-round (8) and total (16) tool-call limits, observability event recording. |
| `src/app/agents/instructions.py` | `AGENT_INSTRUCTIONS`: an Indonesian FMCG sales assistant. The backend is authoritative. Inventing prices or stock is forbidden. Claiming an operation succeeded without a successful backend result is forbidden. |
| `src/app/agents/exceptions.py` | Agent domain errors: `AgentAuthenticationError`, `AgentRateLimitError`, `AgentProviderUnavailableError`, `AgentInvalidResponseError`, `AgentRoundLimitError`, `AgentToolCallLimitError`, `AgentError` (with `partial_transcript`/`partial_events`). |
| `src/app/agents/providers/surpluss.py` | `SurplussProvider`: builds the OpenAI-compatible request, sends it via httpx, normalizes the response, retries transient failures, `_wire_message` transcript serialization. |
| `src/app/tools/definitions.py` | `ToolDefinition` frozen + three definitions: `search_products`, `create_draft_order`, `confirm_order` (inline JSON Schema, Indonesian descriptions). |
| `src/app/tools/registry.py` | `ToolRegistry`: a name→definition+handler map that rejects duplicates and unknown names. `openai_tools()` emits the wire format. |
| `src/app/tools/arguments.py` | `SearchProductsArgs`/`CreateDraftOrderArgs`/`ConfirmOrderArgs` (strict Pydantic, `extra="forbid"`) + `parse_tool_arguments`. |
| `src/app/tools/context.py` | `ToolContext` frozen: `customer_id`, `conversation_id`, `approved_order_id` (single-order approval), `with_confirm_order_approval(order_id)`. |
| `src/app/tools/results.py` | `ToolResult` (ok + exactly one payload), `ToolResultError` (category + message + detail). |
| `src/app/tools/handlers.py` | Three handlers + `ToolServices` (service injection) + `map_service_error` (14 service exceptions → categories). |
| `src/app/tools/executor.py` | `ToolExecutor.execute` always returns a `ToolResult` and never lets an exception cross the boundary. `build_tool_executor(services)` is the wiring point. |

## The three tools

- `search_products` (`search_term`) → `ProductInquiryService.search_products` (read-only, all candidates, no stock reservation).
- `create_draft_order` (`items[]`) → `OrderService.create_draft_order`. `customer_id` comes from the context, prices from the backend.
- `confirm_order` (`order_id`) → `OrderService.confirm_order` after two gates: explicit approval (`approved_order_id` == `order_id`) plus draft ownership (`get_order`, then compare `customer_id`).

## The trust boundary

The model produces free text. Its output is not a type. Four separate concepts, never merged: raw arguments (a JSON string) → validated arguments (Pydantic) → trusted context (`ToolContext`) → the internal service call.

The security invariants are baked into the data. No tool definition accepts `customer_id`, prices, or approval arguments. `customer_id` comes only from the trusted context. Unit prices come only from the backend price list. Confirmation approval comes only from `with_confirm_order_approval()`, called by application logic. The model gets a voice, not a signature.

## Failure categories

`unknown_tool`, `malformed_arguments`, `invalid_arguments`, `unauthorized`, `not_found`, `inactive`, `missing_data`, `business_rule`, `execution_error`. A failure is always data (`ToolResult(ok=False, error={code, message, detail})`) that the model reads, never a raw exception. Unexpected failures are sanitized (the exception class name only). `MissingToolHandlerError` raises hard: a definition without a handler is a wiring bug, not a model failure.

## Execution control

Limits are checked before execution, and a batch is never partial. A batch exceeding a limit is rejected whole (`AgentToolCallLimitError`). Hitting the round limit without final text raises `AgentRoundLimitError` (never a fabricated answer).

Retry lives only at the provider boundary, only for transient failures: `AgentRateLimitError` (honor `Retry-After`, cap 8 s), `AgentProviderUnavailableError` (exponential backoff 0.5 s / 1.0 s, max 2 retries). Timeouts, auth failures, and tool executions are never retried. Tool execution has business side effects: `create_draft_order` twice means two drafts.

## Key decisions

- Raw httpx, not the OpenAI SDK: the provider is documented to answer HTTP 200 even for failed requests (the error sits in the body). Success is decided from the body (`error`/`choices`), not the status.
- OpenAI-compatible Chat Completions, the standard format passed through unchanged, no clever transformation. The P2.8 lesson: boringly standards-compliant means zero interop problems.
- Tool JSON Schemas are hand-written, not Pydantic `model_json_schema()`, because Pydantic emits `$defs`/`format: uuid` that are not verified safe across providers. A test guards the wire↔Pydantic sync.
- No `providers/base.py`: an interface with one implementation is an abstraction without a second consumer. A Protocol gets extracted when a second provider exists.
- No automatic retry in the adapter (P2.1). Transient-only retry was added in P2.6, once the limits were understood.
- `ToolExecutor.execute` takes primitives (id, name, arguments, context) so `app.tools` never needs to import `app.agents`.
- `AgentRunResult.transcript` carries no system instructions. It is persisted and replayed as the next `AgentInput`.
- Observability is pure data (`AgentRunEvent`), not logging infrastructure: identity and timing only, no raw arguments, message contents, or credentials.
- Limit and retry configuration via constructor parameters (defaults). Env wiring followed with the application layer.

## Verification results

- Full unit run (`--ignore=tests/integration`): 331 passed.
- DB integration (`RUN_DB_SMOKE=1`): 30 passed, 3 skipped (real PostgreSQL).
- Live smoke (`RUN_AGENT_SMOKE=1`): 3 passed (text, real tool_calls, and the full loop converging, with model `deepseek-v4.1-flash-1017`).
- `ruff check` + `format --check`: clean.

## Core lessons

- **HTTP success is not business success.** The provider sends 200 for a failed request. The truth is in the body.
- **The cheapest trust boundary is the schema, not a checker.** An argument that was never asked for can never be sent.
- **Never validate a blend of trusted and untrusted objects.** Type separation is trust separation.
- **Failure is also data.** The model reads `ok=false` and answers honestly, instead of a dead conversation.
- **Retry is a safety decision, not a convenience.** The deciding question: does the retry mutate business state?
- **Layered verification wins** because each layer owns a different source of uncertainty: unit (logic), DB (SQL and transactions), live (the provider contract and model behavior).

## Open risks at the end of Phase 2

Agent → DB in one live conversation was untested (it needed the application layer and seeded data). Model answer content (Indonesian, numeric honesty) was guided by instructions but not tested automatically. Detecting explicit customer approval ("yes, confirm" → approval) belonged to the channel layer, not the agent. Limit and retry configuration was not yet env-driven.
