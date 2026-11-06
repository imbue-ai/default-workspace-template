A chat whose agent ended its turn to wait on a background task no longer reads as done. The chat is busy: its agent will resume on its own when the task's report arrives.

- Chats have a new status, `background`: the agent is between turns, no permission request is waiting, and a background task it started is still pending. A chat reads stopped, attention, working, background or idle, the first that holds. The chat list draws it as a dashed accent ring that turns slowly (held still under reduced motion); a chat with a turn running keeps the working dot, tasks or not.

- The chat list's green check now waits for the wait to end: a chat that ends its turn to wait on a background task is not marked done, and is marked once it goes idle with nothing pending while you are looking at another chat.

- Above the message box, above the activity line, a wait line appears whenever background tasks are pending: "Waiting on 2 background tasks · 4m 05s", timed from the oldest. Pressing it lists each task with its description and how long it has run.

- When a chat waiting on background tasks moves to another provider, the agent that takes it over gets a table of them, one row per task with its command and start time: which the switch ended for good (the old agent's own) and which will still report (commands started through `run_in_background.py`, whose report says how they ended).

- In a chat the app has shown, sent to or switched since it started, a Claude background task that has finished stops counting once Claude's transcript records its end, even when you stop the turn that the task's completion started or that turn fails; the chat goes straight from waiting to working, never showing done in between, and a task stopped from Claude's own task list, which starts no turn, stops counting a few seconds later. In any other chat it stops counting at the end of Claude's next turn, or when Claude exits.

- The chat app reads each chat's pending tasks from `data/.apps/chat/background_tasks/<chat-id>/` (the markers `run_in_background.py` and Claude's Stop hook write) on the same once-a-second poll that reads model changes (it creates that directory's root when it starts), and rechecks every waiting chat's task processes on each pass, so a task killed for memory returns its chat to idle within a second. A turn that ends to wait shows the wait at once, and a chat already waiting when the chat app starts shows its wait from the app's first answer. The tasks belong to the chat, so after a switch to another agent the new agent shows the same wait.

- Each chat snapshot's `active_agent` carries `is_busy` and `background_tasks`; a task's `source` is `run_in_background` or `harness`, with `harness` naming which one (`claude`). `GET /api/agents` items carry `chat_id`, `is_busy` (a turn in flight, or the chat's pending tasks) and `background_tasks` (the chat's tasks, on its active agent only), which `background_tasks.py list` and `is-busy` read. `GET /api/agents?tracked=true` answers from the agent list the chat app already follows, without asking mngr to discover agents.

- The memory prioritizer treats a chat waiting on one of Claude's own background tasks like one with a turn running, so it is not aged toward being shed: a shed would end the task unreported. A chat waiting only on `run_in_background.py` commands ages as an idle chat does, since the command's runner outlives a shed and its report revives the chat.

- When a command started through `run_in_background.py` is stopped before its runner can report how it ended (Imbue Studio stopping a busy chat's commands to apply an update, or a restart), the next message the chat sends its agent is followed by a one-line notice naming those commands, so the agent knows no report is coming and can start again what it still needs.

- A Claude turn that ends to wait on one of Claude's own background tasks no longer flashes the chat as done for a moment: until Claude's end-of-turn hooks have run (one of them records the task), the chat reads as still working, then as waiting.
