Added OpenAI Codex CLI support to the workspace image.

- Bake codex 0.145.0 into the image (CODEX_VERSION).
- Ship a repo-committed codex private-instructions channel at .codex/AGENTS.md,
  provisioned as codex's global instructions without polluting the shared
  AGENTS.md every harness reads.
- Pull in the codex agent-type dependencies (imbue-mngr-codex).

- The `worktree`, `worker`, and `subskill-worker` create templates now run `uv sync --all-packages` as an extra provision command (cwd = the fresh worktree, before the agent launches). All three land the agent in a fresh git worktree with no `.venv`, so the agent's first `uv run` previously cold-built one mid-task with root-closure scope, racing the agent's own commands. Pairs with the bootstrap's boot-time venv converge for the shared work_dir (which covers the chat/chat_codex agents and the services).

- Restored the faithful AGENTS.md/CLAUDE.md split from the original `add-codex` design: CLAUDE.md is again the pure Claude delta (`@AGENTS.md` include + the TodoWrite/tk claudism, the `.claude/skills` symlink note, and the Memory section, refreshed to the `data/memories/` layout and the restic host-backup survival story). The codex-V1 redo had kept the `@AGENTS.md` header but left the entire generic body inlined below it, so Claude read the shared content twice. Verified line-by-line that everything removed from CLAUDE.md exists in AGENTS.md (verbatim or as the original deliberate genericizations).
