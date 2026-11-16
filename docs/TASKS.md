# Tasks

## Active

- [ ] Add service-layer workflows for order lifecycle transitions: cancellation and delivery (draft creation and confirmation are done).
- [ ] Add API routers for customer, product, inventory, price, credit, and order operations.
- [ ] Add the OpenAI tool and agent layers after core operations are stable.

## Done

- [x] Order repository (implementation, unit tests, integration test) completed and verified 2026-11-16.
- [x] Full connection injection migrated for all repositories (customer, product, inventory, price, order) with unit and integration tests passing 2026-11-16.
- [x] OrderService draft-creation workflow (service layer, domain exceptions, `OrderRepository.update_total_amount`, unit and integration tests) completed and verified 2026-11-16.
- [x] OrderService confirm-order workflow (credit check, inventory reservation, guarded draft→confirmed transition; `get_for_update`/`sum_confirmed_totals`/`mark_confirmed`/`lock_by_product_ids`/`reserve_stock` repository methods and new domain exceptions) completed and verified 2026-11-16.
- [x] Align `customers.credit_limit` with spec: migration 002 (nullable, no default; NULL = not configured → `CreditLimitNotConfiguredError`, 0 = zero limit → `CreditLimitExceededError`) applied to the live DB; `CustomerResponse.credit_limit: Decimal | None`, `CustomerCreate.credit_limit: Money | None = None`; unit and targeted integration tests. Known limitation: `CustomerUpdate` cannot yet express set-to-NULL (no update workflow). Completed and verified 2026-11-16.

## Current verification

- `uv run pytest -q --ignore=tests/integration` → 147 passed (2026-11-16, before the migration 002 change; full regression after it deferred to the next milestone).
- Targeted verification after the migration 002 change (2026-11-16): `uv run pytest tests/test_schemas.py tests/test_customer_repository.py tests/test_order_service.py` → 84 passed, and `RUN_DB_SMOKE=1` targeted integration (customer NULL/zero credit-limit round-trip, order confirm with NULL and zero limit) passed on the live DB with 0 leaked rows.
- `uv run ruff check src tests` passes and `uv run ruff format --check src tests` reports 44 files already formatted (2026-11-16).
- `RUN_DB_SMOKE=1 uv run pytest tests/integration` → 19 passed: customer, inventory, order (repository and service, draft and confirm), price, and product integration tests. The order-service integration test includes a two-thread confirmation race on one customer's drafts where exactly one confirms and the other raises `CreditLimitExceededError` (2026-11-16, live database, throwaway rows cleaned up).

## Working-tree note

The working tree has uncommitted changes: the OrderService draft-creation and confirm-order implementations are complete and verified but uncommitted, awaiting review (`src/app/services/order.py`, `src/app/services/exceptions.py`, `src/app/repositories/customer.py`, `src/app/repositories/inventory.py`, `src/app/repositories/order.py`, and their tests). The migration 002 credit-limit alignment is also uncommitted and verified: untracked `src/app/database/migrations/002_credit_limit_nullable.sql` and `supabase/migrations/20261116000000_credit_limit_nullable.sql`, modified `src/app/schemas/customer.py`, a comment-only change in `src/app/services/order.py`, related test updates, and these docs. There is also a pre-existing unrelated user change to `src/app/config.py` and an untracked `.audit/` folder. Review and commit separately.
