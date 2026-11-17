# Distributor Operations Agent

FastAPI backend for distributor operations: customer/product search, inventory,
prices, credit validation, order lifecycle (draft → confirm/cancel). Phase 2
agent (Surplus Intelligence, OpenAI-compatible) implemented: natural-language
tool calling over the existing services.

## Structure

- `src/app/api`, HTTP routers
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
- `tests/`, pytest

Dependency direction: `api → tools → services → repositories → database`.

## Run

```
cp .env.example .env   # fill SUPABASE_DB_URL
uvicorn app.main:app --reload
```

## Test

```
pytest
```
