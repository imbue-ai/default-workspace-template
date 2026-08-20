Generalized the agent launch wrapper so every harness gets an OOM band, not just claude.

`claude_oom_launch.py` is now `agent_oom_launch.py` and takes the harness binary as its
first argument, execing it in place exactly as before. `.mngr/settings.toml` gives codex
and pi-coding the `command` they were missing: without it those agents launched unbanded,
so earlyoom shed them by kernel score rather than by the user/worker tiering.

Nothing about the band logic changed -- it always resolved the band from the agent's label
rather than from which binary was running, so it was already harness-agnostic in
everything but its name and its hardcoded `execvp("claude")`.

Codex is two processes -- it uses its `command` as the prefix for both its visible
`--remote` TUI and its `app-server` daemon -- so a codex agent now registers two pids
under one agent id. Both are banded, and either being shed produces a shed-ledger record
where before there was none (an unregistered kill reads as an anonymous subprocess).

Two limits are noted in the wrapper, both strictly better than the unbanded status quo.
Shedding lands the right way only probably: killing the daemon takes the TUI with it
(`codex --remote` exits on lost connection), while killing the TUI orphans the daemon --
same band, so earlyoom picks by memory and takes the larger daemon first, which is the
outcome we want but not one we enforce. And `lookup_pid_by_agent_id` returns the first
live match, so the prioritizer's engagement re-tag reaches one of the two.

Not addressed here: the shed *notice* is claude-only (`claude_shed_notice_hook.py` is a
claude SessionStart hook), so a shed codex agent gets a ledger record but no in-session
explanation when it next starts.
