# chat_background_tasks

The background tasks each chat is waiting on: one marker file per pending task
under `data/.apps/chat/background_tasks/<chat-id>/`, and the code that writes and
reads them. A chat with a live marker is busy: its agent ended its turn, but the
task's completion will start the next one.

## Modules

- `markers` -- the `BackgroundTask` record and its `TaskSource`
  (`run_in_background`, a command whose runner outlives the agent and reports how
  it ended to the chat, or `harness`, one of the harness's own tasks, which ends
  with the agent's process, unreported), atomic writes, liveness (a marker names
  the pid whose death makes it stale, and that process's start time where `/proc`
  has it), and the marker root, which resolves to the main checkout's `data/` even
  from a worker's git worktree.
- `listing` -- which chats are busy and on what: the chat app's
  `GET /api/agents?tracked=true` answer, which also counts a turn in flight, or
  the marker files when the chat app cannot answer.

## Consumers

- `system/scripts/background_tasks.py` -- the CLI (`list`, `is-busy`).
- `system/scripts/claude_background_task_hooks.py` -- Claude's Stop and
  SessionStart hooks, which `.claude/settings.json` wires: they write and clear
  the `harness-claude-*` markers. A harness's hook logic lives with its hooks,
  not here; this package holds only what every writer and reader shares.
- The chat app -- reads the markers on its state poller, shows the `background`
  status, and removes a Claude marker once the transcript reports the task over.
- The shell's avatar, which keeps working while a chat is busy.
- `system/services/oom_priority/bin/memory_candidates.py`, which never offers a
  busy chat's agent as safe to stop.
- `.agents/skills/launch-task/scripts/create_worker.py`'s worker idle check.

`system/scripts/run_in_background.py` writes its own markers with a copy of the
layout, since update-self's staged copy of it runs in workspaces that may predate
this package; `run_in_background_test.py` pins the two together.

The hooks and `memory_candidates.py` import this package under a bare `python3`
(no virtualenv), and `create_worker.py` in the isolated environment `uv run`
gives a script, each via an explicit `sys.path` entry, which is why it is
**stdlib-only** and must stay that way.
