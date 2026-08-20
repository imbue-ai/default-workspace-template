Generalized the agent launch wrapper so every harness gets an OOM band, not just claude.

`claude_oom_launch.py` is now `agent_oom_launch.py` and takes the harness binary as its
first argument, execing it in place exactly as before. `.mngr/settings.toml` gives codex
and pi-coding the `command` they were missing: without it those agents launched unbanded,
so earlyoom shed them by kernel score rather than by the user/worker tiering.

Nothing about the band logic changed -- it always resolved the band from the agent's label
rather than from which binary was running, so it was already harness-agnostic in
everything but its name and its hardcoded `execvp("claude")`.

One known limit, noted in the wrapper: codex uses its `command` as the prefix for both its
visible `--remote` TUI and its `app-server` daemon, so a codex agent registers two pids
under one agent id. Both are banded at launch, which is the protection that matters, but
`lookup_pid_by_agent_id` returns the first match, so the prioritizer's engagement re-tag
reaches only one of the two.
