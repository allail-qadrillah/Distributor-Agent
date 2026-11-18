# Project context

## Purpose

`distributor-agent` is a FastAPI backend for distributor operations.

The current domain includes customer, product, inventory, price, credit, and order data. The order lifecycle is defined as draft, confirmed, cancelled, and delivered in the database migration.

## Current implementation

- `src/app/main.py` exposes `GET /health` and mounts the WhatsApp webhook, operator conversation, and outbox routers.
- `src/app/schemas` contains Pydantic request and response models.
- `src/app/database/connection.py` owns Psycopg connection and transaction lifecycle; callers wrap multi-statement work in `transaction()`.
- `src/app/repositories` contains database access for customer, product, inventory, price, and order records. Every public repository method takes `conn: psycopg.Connection` as first argument after `self`, and the supplied connection must use `dict_row`. Repositories own SQL execution and cursor lifecycle only; they never open or close connections or manage transactions.
- `src/app/services` contains the service layer with `OrderService` (draft creation and confirmation) and `ProductInquiryService` (read-only product search with customer-specific pricing and stock); `src/app/services/exceptions.py` holds domain exceptions; `src/app/services/price_list_resolver.py` holds the shared price-list resolution rule (assigned list or GENERAL). `src/app/agents` implements the P2.1 agent foundation: explicit Python orchestrator (`orchestrator.py`), agent instructions (`instructions.py`, Bahasa Indonesia), input/output schemas (`schemas.py`), domain exceptions, and the Surpluss Intelligence provider adapter (`providers/surpluss.py`, OpenAI-compatible chat completions over httpx, the only module that knows the provider wire format; model comes from configuration). Tool calls run end to end: `src/app/tools/executor.py` (`ToolExecutor.execute` always returns a structured `ToolResult`, primitive arguments so the tools package never imports agent code), `src/app/tools/handlers.py` (translation-only handlers over `ProductInquiryService`/`OrderService`), `src/app/tools/results.py`, and the additive read workflow `OrderService.get_order`. P2.5 closes the conversation loop: `src/app/agents/orchestrator.py` runs provider → tool executor → provider rounds until the final text (bounded by `AgentRoundLimitError`), `src/app/agents/schemas.py` carries the full transcript protocol (`AgentMessage` tool role + `tool_call_id`, `AgentRunResult` persistable transcript without system instructions), and `surpluss.py::_wire_message` serializes it to the provider wire format. P2.6 adds execution control: per-round and total tool-call limits checked before any execution (`AgentToolCallLimitError`, never a partial batch), transient-only bounded retry inside the provider adapter (rate limit honors `Retry-After` up to a wait cap, 5xx/transport get exponential backoff; timeouts, auth, and tool executions are never retried), and `AgentRunEvent` observability records on `AgentRunResult.events` (correlation id, response id, tool/call ids, result category, duration, no secrets, no raw arguments). P2.7 verifies the whole order workflow on the real stack (`tests/test_agent_order_flow.py`: real orchestrator/executor/handlers, scripted provider, faked services), full lifecycle with transcript replay, approval and ownership refusals, ambiguity handling, and end-to-end argument smuggling rejection, with zero production-code changes. P2.8 closes the phase with three-layer verification: 331 unit tests, the gated DB integration regression (30 passed), and LIVE provider smoke (3 passed: real model issues valid tool calls from our OpenAI-standard definitions, and the full orchestrator loop converges against the live provider with model `deepseek-v4.1-flash-1017`). Phase 2 is complete and committed (`0051fee`, review wave `56016f1`/`aaaebd3`). Phase 3 (WhatsApp conversations) is implemented on top of it in the working tree (see the bullets below). Engineering notes live in `docs/notes/`.
- Phase 3 (WhatsApp conversations) adds: `src/app/intent/` (SLM intent classifier on a dedicated OpenAI-compatible endpoint, `INTENT_CLASSIFIER_*` settings, no fallback to the agent provider; `IntentGate` owns the allow/handoff policy), `src/app/messaging/` (`identity`, `conversation_service`, `handoff`, `approval`, `outbox`, `processor` `MessageProcessor`, `locking`, `wiring.build_messaging_stack` composition root), `src/app/whatsapp/` (Baileys `messages.upsert` normalization, bridge adapter), API routers `src/app/api/whatsapp.py` (webhook + delivery), `src/app/api/conversations.py` (operator routes), `src/app/api/outbox.py` (dispatch sweep) wired in `src/app/main.py`, conversation/message/outbox/approval repositories, and `src/app/schemas/conversation.py`. The bridge contract is documented in `docs/notes/P3-whatsapp-adapter.md`.
- Phase 3 remediation (2026-11-18, working tree, review wave): `src/app/api/security.py` is the shared-secret auth boundary (`WHATSAPP_WEBHOOK_SECRET` on the webhook routes, `OPERATOR_API_TOKEN` on the conversation/outbox routes; empty secret = 401 in production, allowed in development — boundary only, no operator identity system). `APP_ENVIRONMENT` (default `production`) makes the fail-closed posture the default. The WhatsApp adapter maps bridge 4xx → `failed` (retryable) and 5xx → `unknown` (never auto-resent). Operator transitions take the same per-phone advisory lock as inbound via `operator_lock` (bounded 10s wait → `LockAcquisitionTimeoutError` → API 503). Outbox claims stamp a `claimed_at` lease; stale `sending` rows are swept to `unknown` before every dispatch. Inbound during a `pending_delivery` approval is deferred (stored unread, no agent run) until `reprocess_deferred`. An `identity_mismatch` (phone resolves to a different customer than the conversation link) stops processing and hands off. Agent replay is bounded to the newest N transcript messages (`load_agent_messages`), full transcript still persisted.
- Migrations define the database schema and database-enforced business constraints: `supabase/migrations/20261111000000_initial_schema.sql` (001, immutable, applied), `supabase/migrations/20261116000000_credit_limit_nullable.sql` (002, applied, `customers.credit_limit` nullable, no default), and `supabase/migrations/20261118000000_phase3_conversations.sql` (003, applied: `conversations`, `messages`, `outbox_messages`, `order_approvals` plus `set_updated_at` triggers), and `supabase/migrations/20261118000100_phase3_1_outbox_claim_lease.sql` (004, applied 2026-11-18: additive `outbox_messages.claimed_at` claim-lease column + backfill + partial index `outbox_sending_claimed_idx`). Each migration is stored twice with identical content: `src/app/database/migrations/NNN_name.sql` and `supabase/migrations/YYYYMMDDHHMMSS_name.sql`. There is no migration runner; migrations are applied manually by executing the SQL (e.g. `psql -f`).

## Dependency direction

The intended direction is `api → tools → services → repositories → database`, as documented in `README.md`; the orchestrator in `app.agents` is the current top caller, and `app.tools` never imports `app.agents`. Current code covers repositories, database access, the order-draft, order-confirmation, and product-inquiry service workflows, the complete Phase 2 agent layer (P2.1–P2.8: provider adapter, tool definitions/validation/execution, multi-round orchestrator loop), and the Phase 3 conversation layer. The messaging stack (`src/app/messaging/wiring.py::build_messaging_stack`) is now the production caller that constructs the services, agent stack, and message processing; API routers depend on it. Keep business rules in services.

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

The integration tests create and clean up throwaway rows. They require `SUPABASE_DB_URL` and migrations 001–004 applied. The agent provider smoke test has its own gate (no database needed):

```text
RUN_AGENT_SMOKE=1 uv run pytest tests/integration/test_agent_provider_integration.py -v
```

It requires `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env`; its 3 tests each make real provider requests (text response, live tool calls, full orchestrator loop).

Run the intent-classifier evaluation against the regression dataset (`tests/fixtures/intent_cases.jsonl`, real SLM requests; needs `INTENT_CLASSIFIER_*` credentials in `.env`; not part of the test suite — the offline dataset tests live in `tests/test_intent_dataset.py`):

```text
uv run python scripts/evaluate_intent.py
```

## Configuration

`src/app/config.py` reads `SUPABASE_DB_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL` (default: the documented Surplus Intelligence canonical URL) from `.env`. Missing agent configuration fails fast with `AgentConfigurationError` when the provider is constructed. Phase 3 adds `INTENT_CLASSIFIER_ENABLED`/`_BASE_URL`/`_API_KEY`/`_MODEL`/`_CONFIDENCE_THRESHOLD` (default 0.85)/`_TIMEOUT_SECONDS`/`_MAX_CONTEXT_MESSAGES` and `WHATSAPP_BRIDGE_URL`/`_TOKEN`/`_TIMEOUT_SECONDS`; remediation adds `APP_ENVIRONMENT` (default `production`, enforces production safety: intent gate cannot be disabled, bridge URL requires a token), `WHATSAPP_WEBHOOK_SECRET`/`OPERATOR_API_TOKEN` (shared-secret API auth, `src/app/api/security.py`), `OUTBOX_STALE_SENDING_SECONDS` (default 60), and `AGENT_TRANSCRIPT_MAX_MESSAGES` (default 200). The messaging stack is built lazily on the first webhook call, so a bad stack configuration surfaces as HTTP 503 there, never at startup. Do not record their values in project memory.
