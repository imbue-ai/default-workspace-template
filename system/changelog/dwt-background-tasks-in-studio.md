A chat now knows when its agent is waiting on a background task, so a chat whose agent ended its turn to wait for a command no longer reads as done. Each pending task is one marker file under `data/.apps/chat/background_tasks/<chat-id>/`, keyed by chat so a handoff moves nothing. A marker names the process whose death makes it stale, and on Linux that process's start time, so a runner killed for memory stops counting at once and a process given the same pid after a restart does not revive it.

- `run_in_background.py` writes its marker before it returns, so the chat is busy before the agent can end its turn, and removes it once the report is delivered or given up on. A new run also removes its chat's markers from runners that died without removing their own. `--chat-id` puts the marker on the named chat along with the report. The new `--keep-host-awake` flag touches the caller's agent activity file every minute while the command runs; it matters only where mngr's idle mode is on.

- Claude's Stop hook now runs `background_tasks.py record-claude-stop`, which copies the shell, monitor, workflow and subagent tasks Claude Code reports at the end of a turn into markers. Each Stop keeps a still-listed task's start time and removes the tasks no longer listed; the SessionStart hook (startup and resume) runs `clear-claude`, since a new Claude process has none of the old one's tasks. Only the agent's main Claude session writes them.

- `python3 system/scripts/background_tasks.py list` and `is-busy <chat-id>` say which chats are waiting on what. They ask the chat app first and read the marker files when it cannot answer.

- A worker running in its own git worktree writes its markers into the main checkout's data dir, where the chat app reads them.
