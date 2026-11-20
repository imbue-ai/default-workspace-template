The `agentic-browser-fleet` skill is renamed `use-browser`, and its command is renamed `browser`, the way the `use-screen` skill's command is `screen`. The old names no longer exist. Replace `uv run --no-sync agentic-browser-fleet <verb>` with `uv run --no-sync browser <verb>`. There is no alias.

- The verbs (`ls`, `new`, `close`, `acquire`, `release`, `handoff`) and their exit codes are unchanged.

- The skill's text changes only where it names itself or its command. Its restructure is separate work, so it keeps its `size` exemption.

- `connect-external-service`, `use-screen` and `publish-template`'s readme recipe name the skill and the command by the new names.
