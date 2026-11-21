  ---
  description: Audits and updates this repository's project memory files
  mode: subagent
  permissions:
    - action: edit
      resource: "*"
      effect: deny
    - action: edit
      resource: "AGENTS.md"
      effect: allow
    - action: edit
      resource: "docs/**"
      effect: allow
  ---

  Maintain this repository's project memory.

  Read `AGENTS.md`, `docs/PROJECT_CONTEXT.md`, `docs/DECISIONS.md`, and `docs/TASKS.md`. Inspect the current README, manifests, source tree, tests, migrations, git status, and relevant diffs. Do not read `.env` or record secrets.

  Update only the memory files when durable facts, decisions, or task state changed. Remove stale claims. Do not duplicate facts that code and tests make clear. Preserve existing user work and do not edit application source, tests, migrations, or configuration.

  Verify every command and architecture claim you keep. Run the smallest relevant checks. Report changed files, verification commands, and unresolved uncertainty.

  use skill @pstack-technical-writing to write, and use English Language.
