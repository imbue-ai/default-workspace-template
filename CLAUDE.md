@AGENTS.md

# Claude

Claude Code's built-in `TodoWrite` is disabled, use tk instead. Step records (`tk create --step "..."`) are the replacement for `TodoWrite`.

`.claude/skills` does not exist, it is symlinked to `.agents/skills/`.

The pytest-timeout note in AGENTS.md (`PYTEST_MAX_DURATION_SECONDS`) refers to the **Bash tool** timeout.

# Memory

Use Claude's built-in memory system. Your memory directory is `data/memories/` (configured via `autoMemoryDirectory` in `.claude/settings.json`).
Memory is gitignored (everything under `data/` is). It survives container loss via the restic `host-backup` service, which snapshots the whole home tree.
