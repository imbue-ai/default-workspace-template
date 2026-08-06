No build or CI tooling changes; this entry covers root-level file edits made alongside the
create-template harness/role split.

CLAUDE.md is now `@AGENTS.md` plus only the four notes that are genuinely
Claude-specific -- TodoWrite being disabled, the `.claude/skills` symlink,
`PYTEST_MAX_DURATION_SECONDS` meaning the Bash tool timeout, and the memory
directory. Everything else in it was a verbatim copy of AGENTS.md, loaded twice.

Root `pyproject.toml` now registers three more harness plugins in the workspace
`.venv` alongside claude/codex -- `imbue-mngr-opencode`, `imbue-mngr-pi-coding`,
and `imbue-mngr-antigravity` (deps + editable `[tool.uv.sources]` paths) -- so
`uv run mngr` parses their agent-type config fields. `uv.lock` is regenerated to
include the three (no other pins change); both build stages run `uv sync --frozen`,
so the lock had to move in lockstep.
