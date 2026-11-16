# Decisions

## 2026-11-16

- Keep project memory split by purpose. `AGENTS.md` contains mandatory agent rules, `docs/PROJECT_CONTEXT.md` contains stable facts, `docs/DECISIONS.md` contains durable choices, and `docs/TASKS.md` contains active work state.
- Keep database connection lifecycle in `src/app/database/connection.py`. Repository methods use parameterized SQL and let the caller own transactions where a workflow spans multiple writes.
- Keep database-enforced invariants in the Supabase migration. Put cross-row workflow checks, such as requiring order items before confirmation, in services when those services are implemented.
- Keep the OpenAI agent loop deferred until the core operations and order lifecycle are implemented.
- Approved Full Connection Injection: all repositories receive their connection from the caller, for read methods as well as write methods. This extends the caller-owned-transactions rule above from multi-write workflows to every database operation. `transaction()` (or a future approved connection manager) owns connection creation, boundaries, commit, and rollback. Repositories own SQL execution and cursor lifecycle only; they never import `get_connection`, never open or close connections, and never call `commit()`, `rollback()`, or `BEGIN`.
