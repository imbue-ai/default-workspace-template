- Fixed `autoMemoryDirectory` in `.claude/settings.json`: the repo-relative
  `data/memories` value is silently ignored by Claude Code (memory fell back
  to `~/.claude/projects/<slug>/memory/`, so `MEMORY.md` never loaded in a new
  chat). It is now the honored `~/workspace/data/memories`; the
  absolute-or-`~`-rooted constraint is documented in CLAUDE.md and pinned by
  `system/scripts/claude_memory_settings_test.py`.
