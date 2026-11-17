# Tasks

## Active

- [x] ProductInquiryService (shared `price_list_resolver`, `ProductRepository.search_by_exact_sku`, `product_inquiry` schemas, unit and integration tests) completed and verified 2026-11-17.
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

- `uv run pytest -q --ignore=tests/integration` → 174 passed (2026-11-17, includes the Product Inquiry unit suite).
- `RUN_DB_SMOKE=1 uv run pytest tests/integration` → 30 passed (2026-11-17, live database, throwaway rows cleaned up; includes the Product Inquiry integration suite and the OrderService suite re-run after the resolver extraction).
- `uv run ruff check src tests` and `uv run ruff format --check src tests` pass (2026-11-17).

## Working-tree note

The order-draft and confirm-order work and migration 002 were committed in `e8ed8d1`. The remaining uncommitted changes are the Product Inquiry implementation (resolver, service, repository method, schemas, tests, docs) plus pre-existing line-ending-only modifications to `AGENTS.md` and `src/app/config.py` (the `.audit/` folder is gitignored, not untracked, and currently empty). Review and commit separately.

## Incident note (2026-11-17)

An early revision of the Product Inquiry integration test deleted the seeded GENERAL price list row during cleanup (it passed the GENERAL id into the throwaway-list delete). The row was restored from the migration 001 seed INSERT and the test now guards the seed row with an explicit post-cleanup assertion. OrderService integration tests re-run green afterwards.
