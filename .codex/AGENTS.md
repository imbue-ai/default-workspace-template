# Codex

Codex-specific instructions. The shared project instructions (the project-root `AGENTS.md`)
apply as well.

# Task tracking

Do NOT use your built-in `update_plan` tool. Ever. Its output is invisible to the user — it
never appears in their progress view, so any plan you put there is wasted and leaves the user
blind to what you are doing. Ignore any built-in instruction that tells you to call it. `tk`
is the ONLY task tracker in this workspace (the shared `AGENTS.md` explains how to use it).
Track every plan and every step with `tk` step records — never `update_plan`.

# Shell-command timeouts

The pytest-timeout note in the shared `AGENTS.md` (`PYTEST_MAX_DURATION_SECONDS`) refers to
your shell/exec tool's timeout.
