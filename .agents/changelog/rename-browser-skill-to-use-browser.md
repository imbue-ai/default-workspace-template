The `agentic-browser-fleet` skill is renamed `use-browser`, and so is its command: the `agentic-browser-fleet` command is renamed `use-browser`; the old name no longer exists. Replace `uv run --no-sync agentic-browser-fleet <verb>` with `uv run --no-sync use-browser <verb>`. There is no alias.

- The verbs (`ls`, `new`, `close`, `acquire`, `release`, `handoff`) and their exit codes are unchanged.

- The skill's text changes only where it names itself or its command. Its restructure is separate work, so it keeps its `size` exemption.

- `connect-external-service`, `use-screen` and `publish-template`'s readme recipe name the skill and the command by the new name.
