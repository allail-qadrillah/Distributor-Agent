# Distributor Operations Agent

FastAPI backend for distributor operations: customer/product search, inventory,
prices, credit validation, order lifecycle (draft → confirm/cancel).
OpenAI agent layer planned as phase 2.

## Structure

- `src/app/api` — HTTP routers
- `src/app/schemas` — Pydantic DTOs
- `src/app/services` — business rules
- `src/app/repositories` — SQL per entity
- `src/app/database` — pool, transactions, migrations
- `src/app/tools` — OpenAI tool specs (phase 2)
- `src/app/agents` — agent loop (phase 2)
- `tests/` — pytest

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
