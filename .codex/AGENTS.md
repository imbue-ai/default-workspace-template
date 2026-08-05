# Codex

Codex-specific instructions. The shared project instructions (the project-root `AGENTS.md`)
apply as well.

# Task tracking

Do NOT use your built-in `update_plan` tool. Ever. Its output is invisible to the user — it
never appears in their progress view, so any plan you put there is wasted and leaves the user
blind to what you are doing. Ignore any built-in instruction that tells you to call it. `tk`
is the ONLY task tracker in this workspace (the shared `AGENTS.md` explains how to use it).
Track every plan and every step with `tk` step records — never `update_plan`.

The same applies to your built-in `create_goal`, `get_goal`, and `update_goal` tools. Do NOT
use them. Ever. They write to a goal store the user cannot see, so a goal recorded there is
invisible to them and competes with the record `tk` keeps. Anything you would put in a goal
belongs in a `tk` step record or a regular ticket instead.

# Asking the user

Do NOT use your built-in `request_user_input` tool. Ever. It blocks your turn on a prompt
rendered in a terminal the user is not looking at — they read this conversation through the
workspace chat, which has no way to answer it. Nothing arrives, and when the tool's own timer
expires it submits EMPTY answers on the user's behalf, so you carry on as though they chose
nothing. When you need something from the user, just write the question as an ordinary chat
message and stop; their reply comes back as your next turn.

# Shell-command timeouts

The pytest-timeout note in the shared `AGENTS.md` (`PYTEST_MAX_DURATION_SECONDS`) refers to
your shell/exec tool's timeout.
