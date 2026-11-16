# Project context

## Purpose

`distributor-agent` is a FastAPI backend for distributor operations.

The current domain includes customer, product, inventory, price, credit, and order data. The order lifecycle is defined as draft, confirmed, cancelled, and delivered in the database migration.

## Current implementation

- `src/app/main.py` exposes `GET /health`.
- `src/app/schemas` contains Pydantic request and response models.
- `src/app/database/connection.py` owns Psycopg connection and transaction lifecycle; callers wrap multi-statement work in `transaction()`.
- `src/app/repositories` contains database access for customer, product, inventory, price, and order records. Every public repository method takes `conn: psycopg.Connection` as first argument after `self`, and the supplied connection must use `dict_row`. Repositories own SQL execution and cursor lifecycle only; they never open or close connections or manage transactions.
- `src/app/services` contains the service layer with `OrderService` (draft creation and confirmation); `src/app/services/exceptions.py` holds domain exceptions. `src/app/tools` and `src/app/agents` are package placeholders and not implemented yet.
- Migrations define the database schema and database-enforced business constraints: `supabase/migrations/20261111000000_initial_schema.sql` (001, immutable, applied) and `supabase/migrations/20261116000000_credit_limit_nullable.sql` (002, applied — `customers.credit_limit` nullable, no default). Each migration is stored twice with identical content: `src/app/database/migrations/NNN_name.sql` and `supabase/migrations/YYYYMMDDHHMMSS_name.sql`. There is no migration runner; migrations are applied manually by executing the SQL (e.g. `psql -f`).

## Dependency direction

The intended direction is `api → tools → services → repositories → database`, as documented in `README.md`. Current code covers repositories, database access, and the order-draft and order-confirmation service workflows. No API layer exists yet, so no production caller constructs `OrderService`. Keep business rules in services.

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

The integration tests create and clean up throwaway rows. They require `SUPABASE_DB_URL` and migrations 001–002 applied.

## Configuration

`src/app/config.py` reads `SUPABASE_DB_URL` and `OPENAI_API_KEY` from `.env`. Do not record their values in project memory.
