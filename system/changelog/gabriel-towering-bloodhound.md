Bug reports now carry the agents' own logs, not just the workspace services'. Before, the
`--logs` half of a report collected `/var/log/supervisor/*.log` only -- and agents run
under tmux, not supervisord, so no harness output reached a report at all. A report told
you what a chat said and nothing about why its harness misbehaved. Under the same "include
logs" consent, the archive now also holds `agent-logs/<agent>/<file>`: each agent's harness
log files, which is codex's `app_server.log` and TUI tail, antigravity's `agy_cli.log`,
opencode's `opencode_server.log`, and the `stderr.log` the claude and pi-coding harnesses
now write.

Each running agent also contributes a capture of its tmux pane, for the harnesses whose
crash output only ever reaches the screen. That one needs BOTH consents rather than the
logs one alone: a TUI harness renders the conversation into its pane, so the scrollback is
chat content as much as diagnostics, and a user who asks for logs while declining to send
their chats has declined it too. The capture passes `--no-start`, so filing a report never
wakes a stopped agent.

The agent logs are scanned and released or withheld as their own class, so a finding in
one no longer costs the report its service logs or its chats.

Two limits changed to go with it. Each log class now fills to a total byte budget
newest-first, so one busy harness cannot hand the secret scanner more plaintext than it
can read inside the host's budget -- without which the report would time out rather than
arrive trimmed. Panes get their own budget rather than competing with the log files: they
are captured at collection time, so under a shared newest-first budget they would sort
above every real harness log and displace the logs of the agent that actually broke. And
the per-file line cap rose from 200 to 2000: the per-file read cap was already what
bounded a member, so the line cap was discarding lines that had been read, and a chatty
service can write 200 lines in under a second.
