# Project instructions

Read `docs/PROJECT_CONTEXT.md` before changing project structure or runtime behavior.
Read relevant entries in `docs/DECISIONS.md` and `docs/TASKS.md` before continuing related work.

Keep project memory factual and short. Update the matching `docs/` file when a durable architecture fact, decision, or task state changes. Do not store secrets or copy facts that code and tests already make clear.

Verify documented commands and architecture claims against the repository before recording them.

## Memory maintenance protocol

At task start, classify expected memory impact from the user request before inspecting or editing code.

Treat a change as memory-relevant when it changes architecture, dependency direction, runtime commands, configuration, durable constraints, technical decisions, active task state, or verification status.

For a task with no expected memory impact, do not reread all memory files after implementation. Run the normal verification, then check only whether implementation changed the predicted scope. Report `project memory unchanged` when it did not.

For a task with expected memory impact, record the affected memory file in the task plan and invoke `project-memory-maintainer` after implementation and verification. The maintainer may edit only `AGENTS.md` and `docs/**`.

If implementation reveals an unplanned architecture, command, configuration, constraint, decision, task, or verification change, invoke `project-memory-maintainer` before reporting completion.

Do not invoke the maintainer for documentation-only wording changes unless they alter a project fact, rule, decision, task, or verification result.

## Enforced rules

Each rule names the check that fails before the mistake ships. Add a check at the highest enforcement level when a new rule is needed.

| Rule | Enforcement | File |
|---|---|---|
| Migrations must be identical in `src/app/database/migrations/` and `supabase/migrations/` | Test (SHA-256 hash comparison) | `tests/test_migration_identity.py` |
| Only `app.database.connection` may call `psycopg.connect()` — all other modules use `connect()`/`transaction()` (guarantees `dict_row` row factory) | Test (AST import/call audit) | `tests/test_connection_discipline.py` |
| No Python source file may contain CRLF line endings | Test (byte scan) | `tests/test_line_endings.py` |
| Seed rows (e.g. GENERAL price list from migration 001) must survive all test cleanup | Integration test (DB smoke gate) | `tests/integration/test_seed_integrity.py` |
| SQL in repositories must match test fakes (attempts increment, jsonb Json() wrap, newest-N window) | Integration test (`RUN_DB_SMOKE=1`) | `tests/integration/` (existing) |
| `map_service_error` must handle every `ServiceError` subclass | Test (`__subclasses__()` exhaustiveness) | `tests/test_tool_handlers.py` |
| `ARGUMENT_MODELS` keys must match `ToolRegistry.names()` | Test (set equality) | `tests/test_tool_arguments.py` |
