# Distributor Operations Agent

FastAPI backend for distributor operations: customer/product search, inventory,
prices, credit validation, order lifecycle (draft → confirm/cancel). Phase 2
agent (Surplus Intelligence, OpenAI-compatible) implemented: natural-language
tool calling over the existing services. Phase 3: persistent WhatsApp
conversations — intent gate (separate SLM, fail-closed), human handoff, order
approval bound to delivered summaries, outbound outbox with delivery
reconciliation. API surface is authenticated with shared secrets (constant-time,
fail closed in production).

## Structure

- `src/app/api`, HTTP routers (incl. WhatsApp webhooks, conversation
  operator routes, outbox dispatch)
- `src/app/schemas`, Pydantic DTOs
- `src/app/services`, business rules
- `src/app/repositories`, SQL per entity
- `src/app/database`, pool, transactions, migrations
- `src/app/tools`, model-visible tools: OpenAI function definitions,
  registry, strict argument validation, trusted context, executor with
  structured results (no SQL; services own business rules)
- `src/app/agents`, explicit orchestrator (multi-turn tool loop, limits,
  observability), instructions, schemas, and the Surpluss Intelligence
  provider adapter
- `src/app/intent`, message intent classifier + gate (fail-closed policy)
- `src/app/messaging`, conversation workflow: identity, transcript, handoff,
  approvals, outbox, processor, per-phone locking
- `src/app/whatsapp`, Baileys event normalization + bridge adapter
  (contract: `docs/notes/P3-whatsapp-adapter.md`)
- `src/app/observability`, structured logging (`log_event`), redaction,
  correlation-id middleware support (`X-Correlation-ID` contextvar)
- `tests/`, pytest (`tests/integration/` = live-DB smoke tests, gated)

Dependency direction: `api → messaging → tools → services → repositories → database`.

## Run

```
cp .env.example .env   # fill SUPABASE_DB_URL (+ INTENT_CLASSIFIER_* to enable the gate)
uvicorn app.main:app --reload
```

`APP_ENVIRONMENT` defaults to `production` (fail closed): webhook routes
need `WHATSAPP_WEBHOOK_SECRET`, operator routes need `OPERATOR_API_TOKEN`,
and `INTENT_CLASSIFIER_ENABLED=false` / a bridge URL without a token are
refused at startup. `APP_ENVIRONMENT=development` is the documented bypass
for local work.

Logs are structured events (JSON in production, colored console on a dev
TTY). Every request gets a validated `X-Correlation-ID` (generated when
missing) echoed on the response, carried through the message pipeline into
agent runs, sent to the bridge, and returned on delivery reports — one
trace id spans the whole flow. `LOG_LEVEL` (default `INFO`) sets verbosity
for the app and the bridge alike.

## Test

```
pytest                                        # offline unit suite
RUN_DB_SMOKE=1 pytest tests/integration      # live Supabase smoke tests
uv run python scripts/evaluate_intent.py     # intent SLM eval (needs INTENT_CLASSIFIER_*)
```
