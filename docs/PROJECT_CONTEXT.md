# Project context

## Purpose

`distributor-agent` is a FastAPI backend for distributor operations.

The current domain includes customer, product, inventory, price, credit, and order data. The order lifecycle is defined as draft, confirmed, cancelled, and delivered in the database migration.

## Current implementation

- `src/app/main.py` exposes `GET /health` and mounts the WhatsApp webhook, operator conversation, and outbox routers.
- `src/app/schemas` contains Pydantic request and response models.
- `src/app/database/connection.py` owns Psycopg connection and transaction lifecycle. Callers wrap multi-statement work in `transaction()`.
- `src/app/repositories` contains database access for customer, product, inventory, price, and order records. Every public repository method takes `conn: psycopg.Connection` as first argument after `self`, and the supplied connection must use `dict_row`. Repositories own SQL execution and cursor lifecycle only. They never open or close connections or manage transactions.
- `src/app/services` contains the service layer: `OrderService` (draft creation and confirmation), `ProductInquiryService` (read-only product search with customer-specific pricing and stock), domain exceptions (`exceptions.py`), and the shared price-list resolution rule (`price_list_resolver.py`: assigned list or GENERAL).
- `src/app/agents` implements the agent foundation: an explicit Python orchestrator (`orchestrator.py`), agent instructions (`instructions.py`, written in Indonesian), input/output schemas (`schemas.py`), domain exceptions, and the Surpluss Intelligence provider adapter (`providers/surpluss.py`). The adapter is the only module that knows the provider wire format (OpenAI-compatible chat completions over httpx). The model comes from configuration.
- `src/app/tools` runs tool calls end to end: `executor.py` (`ToolExecutor.execute` always returns a structured `ToolResult` and takes primitive arguments, so the tools package never imports agent code), `handlers.py` (translation-only handlers over `ProductInquiryService`/`OrderService`), `results.py`, and the additive read workflow `OrderService.get_order`.
- The conversation loop in `orchestrator.py` runs provider → tool executor → provider rounds until the final text, bounded by `AgentRoundLimitError`. `schemas.py` carries the full transcript protocol: the `AgentMessage` tool role with `tool_call_id`, and `AgentRunResult` with a persistable transcript that excludes system instructions. `surpluss.py::_wire_message` serializes it to the provider wire format.
- Execution control: per-round and total tool-call limits are checked before any execution (`AgentToolCallLimitError`, never a partial batch). The provider adapter retries transient failures only: rate limits honor `Retry-After` up to a wait cap, and 5xx/transport errors get exponential backoff. Timeouts, auth failures, and tool executions are never retried. `AgentRunResult.events` carries `AgentRunEvent` observability records: correlation id, response id, tool/call ids, result category, duration. No secrets and no raw arguments.
- Verification history: P2.7 verifies the whole order workflow on the real stack (`tests/test_agent_order_flow.py`: real orchestrator/executor/handlers, scripted provider, faked services, zero production-code changes). P2.8 closed Phase 2 with 331 unit tests, the gated DB integration regression (30 passed), and live provider smoke (3 passed, model `deepseek-v4.1-flash-1017`). Phase 2 is complete and committed (`0051fee`, review wave `56016f1`/`aaaebd3`). Phase 3 (WhatsApp conversations) is committed on top of it in `b58de9b` (see the bullets below). Engineering notes live in `docs/notes/`.
- Phase 3 (WhatsApp conversations) adds: `src/app/intent/` (SLM intent classifier on a dedicated OpenAI-compatible endpoint, `INTENT_CLASSIFIER_*` settings, no fallback to the agent provider, and `IntentGate` owns the allow/handoff policy), `src/app/messaging/` (`identity`, `conversation_service`, `handoff`, `approval`, `outbox`, `processor` `MessageProcessor`, `locking`, `wiring.build_messaging_stack` composition root), `src/app/whatsapp/` (Baileys `messages.upsert` normalization, bridge adapter), API routers `src/app/api/whatsapp.py` (webhook + delivery), `src/app/api/conversations.py` (operator routes), `src/app/api/outbox.py` (dispatch sweep) wired in `src/app/main.py`, conversation/message/outbox/approval repositories, and `src/app/schemas/conversation.py`. The bridge contract is documented in `docs/notes/P3-whatsapp-adapter.md`.
- Phase 3 remediation (2026-11-18, review wave): `src/app/api/security.py` is the shared-secret auth boundary (`WHATSAPP_WEBHOOK_SECRET` on the webhook routes, `OPERATOR_API_TOKEN` on the conversation/outbox routes). An empty secret answers 401 in production and is allowed in development. This is a boundary only, not an operator identity system. `APP_ENVIRONMENT` (default `production`) makes the fail-closed posture the default. The WhatsApp adapter maps bridge 4xx to `failed` (retryable) and 5xx to `unknown` (never auto-resent). Operator transitions take the same per-phone advisory lock as inbound via `operator_lock`, with a bounded 10s wait that ends in `LockAcquisitionTimeoutError` and API 503. Outbox claims stamp a `claimed_at` lease, and stale `sending` rows are swept to `unknown` before every dispatch. Inbound during a `pending_delivery` approval is deferred (stored unread, no agent run) until `reprocess_deferred`. An `identity_mismatch` (phone resolves to a different customer than the conversation link) stops processing and hands off. Agent replay is bounded to the newest N transcript messages (`load_agent_messages`). The full transcript is still persisted.
- Migrations define the database schema and database-enforced business constraints: `supabase/migrations/20261111000000_initial_schema.sql` (001, immutable, applied), `supabase/migrations/20261116000000_credit_limit_nullable.sql` (002, applied, `customers.credit_limit` nullable, no default), and `supabase/migrations/20261118000000_phase3_conversations.sql` (003, applied: `conversations`, `messages`, `outbox_messages`, `order_approvals` plus `set_updated_at` triggers), and `supabase/migrations/20261118000100_phase3_1_outbox_claim_lease.sql` (004, applied 2026-11-18: additive `outbox_messages.claimed_at` claim-lease column + backfill + partial index `outbox_sending_claimed_idx`). Each migration is stored twice with identical content: `src/app/database/migrations/NNN_name.sql` and `supabase/migrations/YYYYMMDDHHMMSS_name.sql`. There is no migration runner. Migrations are applied manually by executing the SQL (e.g. `psql -f`).
- Phase 4 (WhatsApp transport bridge, 2026-11-19) adds `services/whatsapp-bridge/`: a Node.js 26 ESM service pinned to `@whiskeysockets/baileys` 6.7.24 that is transport only. It owns session persistence (`LocalFileSessionStore`, `auth/creds.json` + `keys.json`), connection lifecycle with disconnect classification and exponential-backoff reconnects, an in-repo QR encoder with terminal pairing, a durable inbound queue (sync atomic writes at the socket-listener boundary, per-message full-key dedup, per-JID FIFO with 5 active conversations, retry/quarantine/global-pause policy), durable outbound messageId→outbox mappings persisted before send, read receipts driven by the webhook `mark_read` ack, delivery reports retried until 2xx, and a 1 GB capacity guard with 80%/60% hysteresis. Its HTTP surface is `POST /messages`, `GET /health`, and `GET /ready`. It holds no business logic and no database access.
- Observability (2026-11-20): `src/app/observability.py` owns structured logging and tracing for the Python service. `log_event(logger, event, **fields)` writes one JSON event per decision (JSON formatter in production or headless, colored console on a dev TTY, `LOG_LEVEL` with INFO default, idempotent root setup in `create_app`). Redaction runs at emission and in the formatters: `log_event` passes event fields through `redact_value` (sensitive keys (`token`/`secret`/`password`/…), phone masking (`+628***7890`), URL credential/query stripping, control-character flattening), and the formatters scrub message and traceback text. The middleware in `main.py` validates or generates `X-Correlation-ID` (`[A-Za-z0-9_-]` ≤64), stores it in a contextvar, echoes it on the response, and logs request start/finish. Correlation flows through the whole pipeline (webhook → processor → orchestrator: `AgentRunEvent.correlation_id` prefers the request trace id, conversation id stays the fallback) and is sent by the WhatsApp adapter to the bridge on `POST /messages`. The bridge (pino, pino-pretty dev-only) generates one correlation id per inbound delivery flow (stable across retries), sends it on webhook events and delivery reports, and persists the outbound correlation id in the delivery mapping. Logging failures never break business flow (`log_event` swallows; formatter errors are isolated by logging's own error handling).
- Containers: the root `Dockerfile` (Python app), `services/whatsapp-bridge/Dockerfile`, and `docker-compose.yml` (private network, port 3001 not published, `./data/whatsapp-bridge` bind mount, 30s stop grace) containerize both services. See `services/whatsapp-bridge/README.md` and `docs/notes/P4-whatsapp-bridge.md`.

## Dependency direction

The intended direction is `api → messaging → tools → services → repositories → database`, as documented in `README.md`. The orchestrator in `app.agents` is the current top caller, and `app.tools` never imports `app.agents`. Current code covers repositories, database access, the order-draft, order-confirmation, and product-inquiry service workflows, the complete Phase 2 agent layer (P2.1–P2.8: provider adapter, tool definitions/validation/execution, multi-round orchestrator loop), and the Phase 3 conversation layer. The messaging stack (`src/app/messaging/wiring.py::build_messaging_stack`) is now the production caller that constructs the services, agent stack, and message processing. API routers depend on it. Keep business rules in services.

## Commands

Run the default test suite with:

```text
uv run pytest -q --ignore=tests/integration
```

Run the WhatsApp bridge test suite (Node, no network, no WhatsApp):

```text
cd services/whatsapp-bridge && npm test
```

Build the containers and validate the compose file:

```text
docker compose config --quiet
docker compose build
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

It requires `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env`. Its 3 tests each make real provider requests (text response, live tool calls, full orchestrator loop).

Run the intent-classifier evaluation against the regression dataset (`tests/fixtures/intent_cases.jsonl`, real SLM requests, and it needs `INTENT_CLASSIFIER_*` credentials in `.env`). It is not part of the test suite. The offline dataset tests live in `tests/test_intent_dataset.py`:

```text
uv run python scripts/evaluate_intent.py
```

## Configuration

`src/app/config.py` reads `SUPABASE_DB_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL` (default: the documented Surplus Intelligence canonical URL) from `.env`. Missing agent configuration fails fast with `AgentConfigurationError` when the provider is constructed. Do not record secret values in project memory.

Phase 3 settings: `INTENT_CLASSIFIER_ENABLED`/`_BASE_URL`/`_API_KEY`/`_MODEL`/`_CONFIDENCE_THRESHOLD` (default 0.85)/`_TIMEOUT_SECONDS`/`_MAX_CONTEXT_MESSAGES` and `WHATSAPP_BRIDGE_URL`/`_TOKEN`/`_TIMEOUT_SECONDS`. The remediation wave adds `APP_ENVIRONMENT` (default `production`, enforcing production safety: the intent gate cannot be disabled, and a bridge URL requires a token), `WHATSAPP_WEBHOOK_SECRET`/`OPERATOR_API_TOKEN` (shared-secret API auth, `src/app/api/security.py`), `OUTBOX_STALE_SENDING_SECONDS` (default 60), and `AGENT_TRANSCRIPT_MAX_MESSAGES` (default 200). The messaging stack is built lazily on the first webhook call, so a bad stack configuration surfaces as HTTP 503 there, never at startup.

The Node bridge (Phase 4) reads its own variables: `WHATSAPP_BRIDGE_DATA_DIR`, `WHATSAPP_BRIDGE_TOKEN`, `WHATSAPP_WEBHOOK_SECRET`, `WHATSAPP_BRIDGE_FASTAPI_URL`, `WHATSAPP_BRIDGE_HOST` (default 127.0.0.1), `WHATSAPP_BRIDGE_PORT` (default 3001), and `WHATSAPP_BRIDGE_QR_MODE` (`local` or `docker`). The bridge fails closed when the token, the webhook secret, or the FastAPI URL is missing.

`LOG_LEVEL` (default `INFO`) sets the log verbosity for both services; the app accepts Python level names (`DEBUG|INFO|WARNING|ERROR|CRITICAL`), the bridge accepts pino names (`fatal|error|warn|info|debug|trace`, validated fail-closed). Both log JSON outside a dev TTY; no ANSI codes reach structured output.
