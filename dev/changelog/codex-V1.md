Added OpenAI Codex CLI support to the workspace image.

- Bake codex 0.145.0 into the image (CODEX_VERSION).
- Ship a repo-committed codex private-instructions channel at .codex/AGENTS.md,
  provisioned as codex's global instructions without polluting the shared
  AGENTS.md every harness reads.
- Pull in the codex agent-type dependencies (imbue-mngr-codex).
