---
description: Run a named skill from .agents/skills (explicit invocation only)
---
Read `.agents/skills/$1/SKILL.md` and follow it for the rest of this task.

When that skill references another skill by name only (no path), read `.agents/skills/<that name>/SKILL.md` and follow it too. Resolve relative paths from the directory of the skill that references them.

Task: $ARGUMENTS
