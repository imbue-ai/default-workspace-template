A chat now knows when its agent is waiting on a background task, so a chat whose agent ended its turn to wait for a command no longer reads as done. Each pending task is one marker file under `data/.apps/chat/background_tasks/<chat-id>/`, keyed by chat so a handoff moves nothing. A marker names the process whose death makes it stale, so a runner killed for memory stops counting at once.

- `run_in_background.py` writes its marker before it returns, so the chat is busy before the agent can end its turn, and removes it once the report is delivered or given up on. `--chat-id` puts the marker on the named chat along with the report. The new `--keep-host-awake` flag touches the caller's agent activity file every minute while the command runs; it matters only where mngr's idle mode is on.

- Claude's Stop hook now runs `background_tasks.py record-claude-stop`, which copies the shell, monitor, workflow and subagent tasks Claude Code reports at the end of a turn into markers. The UserPromptSubmit hook and the SessionStart hook (startup and resume) run `clear-claude`, so a turn starts from an empty list. Only the agent's main Claude session writes them.

- `python3 system/scripts/background_tasks.py list` and `is-busy <chat-id>` say which chats are waiting on what. They ask the chat app first and read the marker files when it cannot answer.

- `.mngr/settings.toml` pins `MINDS_BACKGROUND_TASKS_DIR` to the chat app's data dir, so a worker running in its own worktree writes its markers where the chat app reads them.
