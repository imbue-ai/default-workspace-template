Added OpenAI Codex CLI support to the workspace image.

- Bake codex 0.145.0 into the image (CODEX_VERSION).
- Ship a repo-committed codex private-instructions channel at .codex/AGENTS.md,
  provisioned as codex's global instructions without polluting the shared
  AGENTS.md every harness reads.
- Pull in the codex agent-type dependencies (imbue-mngr-codex).

- The `worktree` create template now runs `uv sync --all-packages` as an extra provision command (cwd = the fresh worktree, before the agent launches). A new worktree has no `.venv`, so the agent's first `uv run` previously cold-built one mid-task with root-closure scope, racing the agent's own commands. Pairs with the bootstrap's boot-time venv converge for the shared work_dir.
