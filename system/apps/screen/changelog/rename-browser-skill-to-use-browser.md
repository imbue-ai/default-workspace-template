The `agentic-browser-fleet` command is renamed `use-browser`; the old name no longer exists. Replace `uv run --no-sync agentic-browser-fleet <verb>` with `uv run --no-sync use-browser <verb>`. There is no alias. The verbs, their output and their exit codes are unchanged.

The messages the Screen prints that tell an agent what to run (a stopped browser, a lost lease, a browser that is not yours, a CDP call that would close the last page, a missing `MNGR_AGENT_ID`) name `use-browser`, as does the README.
