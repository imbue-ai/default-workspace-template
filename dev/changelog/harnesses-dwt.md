No build or CI tooling changes; this entry covers root-level file edits made alongside the
create-template harness/role split.

CLAUDE.md is now `@AGENTS.md` plus only the four notes that are genuinely
Claude-specific -- TodoWrite being disabled, the `.claude/skills` symlink,
`PYTEST_MAX_DURATION_SECONDS` meaning the Bash tool timeout, and the memory
directory. Everything else in it was a verbatim copy of AGENTS.md, loaded twice.
