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
