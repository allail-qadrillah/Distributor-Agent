# Tasks

## Active

- [ ] Add service-layer workflows for order lifecycle transitions.
- [ ] Add API routers for customer, product, inventory, price, credit, and order operations.
- [ ] Add the OpenAI tool and agent layers after core operations are stable.

## Done

- [x] Order repository (implementation, unit tests, integration test) completed and verified 2026-11-16.
- [x] Full connection injection migrated for all repositories (customer, product, inventory, price, order) with unit and integration tests passing 2026-11-16.

## Current verification

- `uv run pytest -q --ignore=tests/integration` → 81 passed (2026-11-16, post-migration).
- `uv run ruff check src tests` passes and `uv run ruff format --check src tests` reports 40 files already formatted (2026-11-16).
- `RUN_DB_SMOKE=1 uv run pytest tests/integration -v` → 5 passed: customer, inventory, order, price, product integration tests (2026-11-16, live database).

## Working-tree note

The working tree has uncommitted changes: all five repository modules, schemas, and tests are modified; the order repository files and its tests are untracked. Review and commit separately.
