Bug reports now carry the agents' own logs, not just the workspace services'. Before, the
`--logs` half of a report collected `/var/log/supervisor/*.log` only -- and agents run
under tmux, not supervisord, so no harness output reached a report at all. A report told
you what a chat said and nothing about why its harness misbehaved. Under the same "include
logs" consent, the archive now also holds `agent-logs/<agent>/<file>`: each agent's harness
log files (codex's `app_server.log` and TUI tail, antigravity's `agy_cli.log`, and the
`stderr.log` the claude, pi-coding and opencode harnesses now write), plus a capture of
each running agent's tmux pane for the harnesses whose only record is what is on screen.
The pane capture passes `--no-start`, so filing a report never wakes a stopped agent.

The agent logs are scanned and released or withheld as their own class, so a finding in
one no longer costs the report its service logs or its chats.

Two limits changed to go with it. Each log class now fills to a total byte budget
newest-first, so one busy harness cannot hand the secret scanner more plaintext than it
can read inside the host's budget -- without which the report would time out rather than
arrive trimmed. And the per-file line cap rose from 200 to 2000: the per-file read cap was
already what bounded a member, so the line cap was discarding lines that had been read, and
a chatty service can write 200 lines in under a second.
