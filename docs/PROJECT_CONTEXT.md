# Project context

## Purpose

`distributor-agent` is a FastAPI backend for distributor operations.

The current domain includes customer, product, inventory, price, credit, and order data. The order lifecycle is defined as draft, confirmed, cancelled, and delivered in the database migration.

## Current implementation

- `src/app/main.py` exposes `GET /health`.
- `src/app/schemas` contains Pydantic request and response models.
- `src/app/database/connection.py` owns Psycopg connection and transaction lifecycle; callers wrap multi-statement work in `transaction()`.
- `src/app/repositories` contains database access for customer, product, inventory, price, and order records. Every public repository method takes `conn: psycopg.Connection` as first argument after `self`, and the supplied connection must use `dict_row`. Repositories own SQL execution and cursor lifecycle only; they never open or close connections or manage transactions.
- `src/app/services` contains the service layer with `OrderService` (draft creation and confirmation) and `ProductInquiryService` (read-only product search with customer-specific pricing and stock); `src/app/services/exceptions.py` holds domain exceptions; `src/app/services/price_list_resolver.py` holds the shared price-list resolution rule (assigned list or GENERAL). `src/app/agents` implements the P2.1 agent foundation: explicit Python orchestrator (`orchestrator.py`), agent instructions (`instructions.py`, Bahasa Indonesia), input/output schemas (`schemas.py`), domain exceptions, and the Surpluss Intelligence provider adapter (`providers/surpluss.py`, OpenAI-compatible chat completions over httpx, the only module that knows the provider wire format; model comes from configuration). Tool calls run end to end: `src/app/tools/executor.py` (`ToolExecutor.execute` always returns a structured `ToolResult`, primitive arguments so the tools package never imports agent code), `src/app/tools/handlers.py` (translation-only handlers over `ProductInquiryService`/`OrderService`), `src/app/tools/results.py`, and the additive read workflow `OrderService.get_order`. P2.5 closes the conversation loop: `src/app/agents/orchestrator.py` runs provider → tool executor → provider rounds until the final text (bounded by `AgentRoundLimitError`), `src/app/agents/schemas.py` carries the full transcript protocol (`AgentMessage` tool role + `tool_call_id`, `AgentRunResult` persistable transcript without system instructions), and `surpluss.py::_wire_message` serializes it to the provider wire format. P2.6 adds execution control: per-round and total tool-call limits checked before any execution (`AgentToolCallLimitError`, never a partial batch), transient-only bounded retry inside the provider adapter (rate limit honors `Retry-After` up to a wait cap, 5xx/transport get exponential backoff; timeouts, auth, and tool executions are never retried), and `AgentRunEvent` observability records on `AgentRunResult.events` (correlation id, response id, tool/call ids, result category, duration, no secrets, no raw arguments). P2.7 verifies the whole order workflow on the real stack (`tests/test_agent_order_flow.py`: real orchestrator/executor/handlers, scripted provider, faked services), full lifecycle with transcript replay, approval and ownership refusals, ambiguity handling, and end-to-end argument smuggling rejection, with zero production-code changes. P2.8 closes the phase with three-layer verification: 331 unit tests, the gated DB integration regression (30 passed), and LIVE provider smoke (3 passed: real model issues valid tool calls from our OpenAI-standard definitions, and the full orchestrator loop converges against the live provider with model `deepseek-v4.1-flash-1017`). Phase 2 is complete and uncommitted, awaiting the owner's commit. Phase 3 (WhatsApp/API application layer) is next. Engineering notes live in `docs/notes/`.
- Migrations define the database schema and database-enforced business constraints: `supabase/migrations/20261111000000_initial_schema.sql` (001, immutable, applied) and `supabase/migrations/20261116000000_credit_limit_nullable.sql` (002, applied, `customers.credit_limit` nullable, no default). Each migration is stored twice with identical content: `src/app/database/migrations/NNN_name.sql` and `supabase/migrations/YYYYMMDDHHMMSS_name.sql`. There is no migration runner; migrations are applied manually by executing the SQL (e.g. `psql -f`).

## Dependency direction

The intended direction is `api → tools → services → repositories → database`, as documented in `README.md`; the orchestrator in `app.agents` is the current top caller, and `app.tools` never imports `app.agents`. Current code covers repositories, database access, the order-draft, order-confirmation, and product-inquiry service workflows, and the complete Phase 2 agent layer (P2.1–P2.8: provider adapter, tool definitions/validation/execution, multi-round orchestrator loop). No API layer exists yet, so no production caller constructs `OrderService`, `ProductInquiryService`, or `AgentOrchestrator`. Keep business rules in services.

## Commands

Run the default test suite with:

```text
uv run pytest -q --ignore=tests/integration
```

Run the read-only database connectivity check with:

```text
uv run python scripts/check_db_connection.py
```

Run a live integration smoke test only when Supabase is configured and the test is explicitly selected:

```text
RUN_DB_SMOKE=1 uv run pytest tests/integration/<test_file>.py -v
```

The integration tests create and clean up throwaway rows. They require `SUPABASE_DB_URL` and migrations 001–002 applied. The agent provider smoke test has its own gate (no database needed):

```text
RUN_AGENT_SMOKE=1 uv run pytest tests/integration/test_agent_provider_integration.py -v
```

It requires `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env`; its 3 tests each make real provider requests (text response, live tool calls, full orchestrator loop).

## Configuration

`src/app/config.py` reads `SUPABASE_DB_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL` (default: the documented Surplus Intelligence canonical URL) from `.env`. Missing agent configuration fails fast with `AgentConfigurationError` when the provider is constructed. Do not record their values in project memory.
