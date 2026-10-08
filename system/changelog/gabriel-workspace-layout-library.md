The agent-facing desktop command is now `uv run --no-sync workspace-layout` (from the new `system/libs/workspace_layout` library) instead of `python3 system/scripts/layout.py`, which is removed; the root project depends on the library so the command is always in the root venv.

- AGENTS.md, the automation agents' system prompt, and the docs name the new command.

- `refresh_workspace_view.py` stays stdlib-only; a test now checks the body it posts against the shell's own request models.

- The desktop contract's op route table gives `place` a `state` or a `frame` record and a shortcut op's `cell` as a record, and says the arguments and answers are the `workspace_layout` models.
