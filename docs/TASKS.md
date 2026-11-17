# Tasks

## Active

- [x] ProductInquiryService (shared `price_list_resolver`, `ProductRepository.search_by_exact_sku`, `product_inquiry` schemas, unit and integration tests) completed and verified 2026-11-17.
- [x] P2.1 Agent Foundation selesai dan terverifikasi 2026-11-17: provider Surpluss Intelligence + `glm-5.3`, adapter httpx, orkestrator eksplisit, instruksi, skema, 47 unit test, smoke test opt-in `RUN_AGENT_SMOKE=1` (jalur error live terverifikasi; happy path menunggu `OPENAI_API_KEY` di `.env`). Catatan: `docs/notes/P2.1-agent-foundation.md`.
- [x] P2.2, definisi tool dan registry selesai dan terverifikasi 2026-11-17: 3 definisi (search_products, create_draft_order, confirm_order) format OpenAI standar tanpa field customer_id/harga/approval, `ToolRegistry` (tolak duplikat & tak dikenal), `complete(messages, tools)` di adapter, 21 unit test. Catatan: `docs/notes/P2.2-tool-definition-registry.md`.
- [x] P2.3, konteks terpercaya + validasi argumen selesai dan terverifikasi 2026-11-17: `ToolContext` (customer_id dari aplikasi, approval konfirmasi default False), model argumen Pydantic v2 strict (`extra="forbid"`), `parse_tool_arguments` dengan penolakan malformed/invalid/unknown, test sinkronisasi wire↔Pydantic. Catatan: `docs/notes/P2.3-context-argument-validation.md`.
- [x] P2.4, eksekusi tool + integrasi service selesai dan terverifikasi 2026-11-17: `ToolExecutor` (registry → validasi → handler → `ToolResult` terstruktur selalu), tiga handler ke `ProductInquiryService`/`OrderService`, `OrderService.get_order` read workflow aditif untuk cek kepemilikan, `map_service_error` (13 eksepsi → kategori). Catatan: `docs/notes/P2.4-tool-execution.md`.
- [x] P2.5, loop orkestrasi multi-putaran selesai dan terverifikasi 2026-11-17: `AgentMessage` protokol transkrip penuh (peran `tool` + `tool_call_id`, assistant `content` XOR `tool_calls`), `AgentOrchestrator.run` loop provider→executor→provider sampai teks akhir / `AgentRoundLimitError` (default 5 putaran), `AgentRunResult.transcript` persistable tanpa instruksi sistem, `_wire_message` provider, executor refactor ke primitif (tools tidak impor agents). Catatan: `docs/notes/P2.5-tool-result-orchestration.md`.
- [x] P2.6, kendali eksekusi + error handling selesai dan terverifikasi 2026-11-17: limit panggilan tool per giliran (8) / total (16) dicek SEBELUM eksekusi (`AgentToolCallLimitError`, batch tak pernah parsial), retry transien-saja di provider (429 hormati Retry-After ≤ 8s, 5xx/transport backoff 0,5s→1,0s, maks 2 retry; timeout/auth TANPA retry; eksekusi tool tidak pernah di-retry), `AgentRunEvent` observabilitas (correlation/response/tool/call id, kategori, durasi) di `AgentRunResult.events` tanpa rahasia. Konfigurasi via parameter konstruktor, env wiring menyusul bersama lapisan aplikasi. Catatan: `docs/notes/P2.6-error-handling-execution-control.md`.
- [x] P2.7, keamanan alur pesanan lewat agen selesai dan terverifikasi 2026-11-17: 7 test integrasi jalur nyata (orkestrator + executor + handler asli; provider di-script, service di-fake), siklus hidup penuh 3 run (cari → draft → approval → konfirmasi) dengan replay transkrip, penolakan tanpa approval (nol panggilan service), draft pelanggan lain, order_id karangan, stok kurang, ambiguitas 2 kandidat (semua kandidat ke model, nol draft), penyelundupan identitas/harga ditolak. Nol perubahan `src/`. Catatan: `docs/notes/P2.7-order-tool-safety.md`.
- [x] P2.8, verifikasi end-to-end selesai 2026-11-17: regresi unit 331 passed, DB smoke 30 passed/3 skipped (3 test agen ter-gate `RUN_AGENT_SMOKE`), live smoke 3 passed (teks + tool_calls nyata + loop orkestrator konvergen terhadap provider hidup, model `deepseek-v4.1-flash-1017`), ruff bersih, 8 catatan engineering lengkap. FASE 2 SELESAI. Catatan: `docs/notes/P2.8-end-to-end-verification.md`.
- [ ] Add service-layer workflows for order lifecycle transitions: cancellation and delivery (draft creation and confirmation are done).
- [ ] Add API routers for customer, product, inventory, price, credit, and order operations.
- [ ] Phase 3 open items from P2.8: agent→DB in one live conversation, answer-content compliance evaluation, env wiring for agent limit/retry settings alongside the application layer.

## Done

- [x] Order repository (implementation, unit tests, integration test) completed and verified 2026-11-16.
- [x] Full connection injection migrated for all repositories (customer, product, inventory, price, order) with unit and integration tests passing 2026-11-16.
- [x] OrderService draft-creation workflow (service layer, domain exceptions, `OrderRepository.update_total_amount`, unit and integration tests) completed and verified 2026-11-16.
- [x] OrderService confirm-order workflow (credit check, inventory reservation, guarded draft→confirmed transition; `get_for_update`/`sum_confirmed_totals`/`mark_confirmed`/`lock_by_product_ids`/`reserve_stock` repository methods and new domain exceptions) completed and verified 2026-11-16.
- [x] Align `customers.credit_limit` with spec: migration 002 (nullable, no default; NULL = not configured → `CreditLimitNotConfiguredError`, 0 = zero limit → `CreditLimitExceededError`) applied to the live DB; `CustomerResponse.credit_limit: Decimal | None`, `CustomerCreate.credit_limit: Money | None = None`; unit and targeted integration tests. Known limitation: `CustomerUpdate` cannot yet express set-to-NULL (no update workflow). Completed and verified 2026-11-16.

## Current verification

- `uv run pytest -q --ignore=tests/integration` → 331 passed; `RUN_DB_SMOKE=1 uv run pytest -q tests/integration` → 30 passed, 3 skipped; `RUN_AGENT_SMOKE=1 uv run pytest tests/integration/test_agent_provider_integration.py` → 3 passed (2026-11-17, fase 2 lengkap: unit + DB + provider/model nyata).
- `RUN_DB_SMOKE=1 uv run pytest tests/integration` → 30 passed, 3 skipped (2026-11-17; yang ter-skip = 3 test smoke agen provider, gate `RUN_AGENT_SMOKE` terpisah, tanpa DB).
- `uv run ruff check src tests` dan `uv run ruff format --check src tests` pass (2026-11-17).
- Jalur error provider live (2026-11-17): kunci palsu via `SurplussProvider` ke endpoint produksi → `AgentAuthenticationError` (`code="invalid_api_key"`, `request_id` nyata).

## Working-tree note

Order work dan migration 002 tercommit di `e8ed8d1`; Product Inquiry tercommit di `2ba30df`. Seluruh fase 2 (P2.1–P2.8) belum tercommit di working tree: `src/app/agents/` (5 modul + provider), `src/app/tools/` (8 modul + `__init__.py`), `OrderService.get_order`, 8 test file agen + `tests/test_order_service.py`, smoke test integrasi provider, `docs/notes/` (8 catatan), pembaruan `docs/` dan `README.md`, `.env.example`, serta `src/app/config.py` (bertambah `openai_model`/`openai_base_url`). Modifikasi `AGENTS.md` milik pengguna hanya line ending CRLF (folder `.audit/` di-gitignore). Review dan commit terpisah.

## Incident note (2026-11-17)

An early revision of the Product Inquiry integration test deleted the seeded GENERAL price list row during cleanup (it passed the GENERAL id into the throwaway-list delete). The row was restored from the migration 001 seed INSERT and the test now guards the seed row with an explicit post-cleanup assertion. OrderService integration tests re-run green afterwards.
