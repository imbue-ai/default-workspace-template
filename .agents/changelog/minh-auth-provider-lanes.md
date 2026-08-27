`claude_p.py` falls back to the workspace's default provider account when
`CLAUDE_CONFIG_DIR` is unset. Inside an agent the variable is always set -- mngr sources the
agent's env file into every process in its tmux session -- so a skill script, a worker and the
agent itself all use the account the chat is bound to. Outside one, in a supervisord service or
a cron job, nothing set it and `~/.claude` holds no credential now that auth lives in accounts,
so every AI-driven app the user built stopped working there. It reads the account index
directly rather than importing the system-interface package, since it runs from whatever
environment its caller has.
