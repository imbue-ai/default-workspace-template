@AGENTS.md

# Memory

Use Claude's built-in memory system. Your memory directory is `~/workspace/data/memories` (i.e. `data/memories/` in the repo, configured via `autoMemoryDirectory` in `.claude/settings.json`).
`autoMemoryDirectory` must be an absolute or `~`-rooted path -- a repo-relative value like `data/memories` is silently ignored and Claude falls back to `~/.claude/projects/<slug>/memory/`, so `MEMORY.md` never loads. Keep it as `~/workspace/data/memories`.
Never save secrets in memory: passwords, API keys, tokens, government ID numbers or financial account numbers, even if asked. Don't save sensitive personal details -- health, race or ethnicity, religious beliefs, political views, sexual orientation or gender identity -- unless the user explicitly asks you to remember them.
If you are told the user deleted or edited a note, their version wins: don't save a deleted note's content again, and read an edited note before you change it.
If you are told workspace memory is off, don't read, use or save memories until you are told it is on again; that overrides the line above about using built-in memory.
Memory is gitignored (everything under `data/` is). It survives container loss via the restic `host-backup` service, which snapshots the whole home tree.
