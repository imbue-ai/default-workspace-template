@AGENTS.md

# Memory

Use Claude's built-in memory system. Your memory directory is `~/workspace/data/memories` (i.e. `data/memories/` in the repo, configured via `autoMemoryDirectory` in `.claude/settings.json`).
`autoMemoryDirectory` must be an absolute or `~`-rooted path -- a repo-relative value like `data/memories` is silently ignored and Claude falls back to `~/.claude/projects/<slug>/memory/`, so `MEMORY.md` never loads. Keep it as `~/workspace/data/memories`.
Memory is gitignored (everything under `data/` is). It survives container loss via the restic `host-backup` service, which snapshots the whole home tree.
