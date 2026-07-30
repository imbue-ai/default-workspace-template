Added OpenAI Codex CLI support to the workspace image.

- Bake codex 0.145.0 into the image (CODEX_VERSION).
- Ship a repo-committed codex private-instructions channel at .codex/AGENTS.md,
  provisioned as codex's global instructions without polluting the shared
  AGENTS.md every harness reads.
- Pull in the codex agent-type dependencies (imbue-mngr-codex).

- The `worktree`, `worker`, and `subskill-worker` create templates now run `uv sync --all-packages` as an extra provision command (cwd = the fresh worktree, before the agent launches). All three land the agent in a fresh git worktree with no `.venv`, so the agent's first `uv run` previously cold-built one mid-task with root-closure scope, racing the agent's own commands. Pairs with the bootstrap's boot-time venv converge for the shared work_dir (which covers the chat/chat_codex agents and the services).

- Restored the faithful AGENTS.md/CLAUDE.md split from the original `add-codex` design: CLAUDE.md is again the pure Claude delta (`@AGENTS.md` include + the TodoWrite/tk claudism, the `.claude/skills` symlink note, and the Memory section, refreshed to the `data/memories/` layout and the restic host-backup survival story). The codex-V1 redo had kept the `@AGENTS.md` header but left the entire generic body inlined below it, so Claude read the shared content twice. Verified line-by-line that everything removed from CLAUDE.md exists in AGENTS.md (verbatim or as the original deliberate genericizations).

- Create templates split into two orthogonal kinds, stacked at create time as
  `mngr create <name> -t <harness> -t <role>`. A **harness** template (`claude`, `codex`)
  sets only `type`; a **role** template (`chat`, `caretaker`, `automation`, `worker`) says
  what the agent is for and never sets `type`, so the same role runs under any harness.
  Adding a harness now costs one template instead of one per role -- previously `chat` and
  `chat_codex` were near-duplicates and the codex one had no output style at all.

- Roles express their system prompt harness-neutrally. `output_style` names the `name:`
  frontmatter of a file in `.agents/output-styles/` (moved there from
  `.claude/output-styles/`, which is now a symlink to it, mirroring `.claude/skills`);
  `append_system_prompt` replaces the hand-written `agent_args` flag pairs. Claude applies
  the style as its native `outputStyle` setting; codex, having no output-style concept, gets
  the same file's body as developer instructions. Codex chat agents therefore pick up the
  Engineering Subordinate style they previously lacked.

- `uv sync --all-packages` now runs for **every** agent, from `[commands.create]`, rather
  than being repeated in individual templates. Agents sharing the workspace work_dir were
  hitting missing-package failures the per-template converge did not cover.

- Removed the worktree agent mode: the "New agent" menu entry, the
  `POST /api/agents/create-worktree` endpoint, `CreateWorktreeRequest`, and the `worktree`
  create template. The `+` menu now offers "New chat" (claude) and "New Codex Agent" (codex),
  which are the same `chat` role on different harnesses. This does not affect
  `/home/user/worktrees/` -- worker sub-agents still run in their own git worktrees.

- Removed the `subskill-worker` template. Its only difference from `worker` was installing
  the generic `harden-worker` sub-skill, which `worker` now does for every worker; the
  crystallize / heal / update creation flows and update-system-interface use
  `--template worker`.

- Removed the `chat` and `worker` agent types. Both existed only to hang role config off
  `parent_type = "claude"`; that config now lives in the role templates, so every claude
  role resolves to the `claude` type itself.
