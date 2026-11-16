# Project context

## Purpose

`distributor-agent` is a FastAPI backend for distributor operations.

The current domain includes customer, product, inventory, price, credit, and order data. The order lifecycle is defined as draft, confirmed, cancelled, and delivered in the database migration.

## Current implementation

- `src/app/main.py` exposes `GET /health`.
- `src/app/schemas` contains Pydantic request and response models.
- `src/app/database/connection.py` owns Psycopg connection and transaction lifecycle; callers wrap multi-statement work in `transaction()`.
- `src/app/repositories` contains database access for customer, product, inventory, price, and order records. Every public repository method takes `conn: psycopg.Connection` as first argument after `self`, and the supplied connection must use `dict_row`. Repositories own SQL execution and cursor lifecycle only; they never open or close connections or manage transactions.
- `src/app/services`, `src/app/tools`, and `src/app/agents` are package placeholders. Business services and the OpenAI agent loop are not implemented yet.
- `supabase/migrations/20261111000000_initial_schema.sql` defines the database schema and database-enforced business constraints.

## Dependency direction

The intended direction is `api → tools → services → repositories → database`, as documented in `README.md`. Current code mainly exercises repositories and database access. Keep business rules in services when that layer is added.

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

The integration tests create and clean up throwaway rows. They require `SUPABASE_DB_URL` and the initial migration.

## Configuration

`src/app/config.py` reads `SUPABASE_DB_URL` and `OPENAI_API_KEY` from `.env`. Do not record their values in project memory.
