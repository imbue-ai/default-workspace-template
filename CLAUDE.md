@AGENTS.md

# Claude

- Claude Code's built-in `TodoWrite` is disabled; step records are the replacement for `TodoWrite`.
- The pytest-timeout note (`PYTEST_MAX_DURATION_SECONDS`) refers to the **Bash tool** timeout.
- **CLAUDE.md** (this file): update these instructions if you discover better ways to operate.
- `.agents/skills/` is also symlinked from `.claude/skills/`.
- `runtime/memory/` holds Claude memory.

# Memory

Use Claude's built-in memory system. Your memory directory is `runtime/memory/` (configured via `autoMemoryDirectory` in `.claude/settings.json`).
