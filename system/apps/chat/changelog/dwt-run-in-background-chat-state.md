A chat whose agent ended its turn to wait on a background task no longer looks finished.

- The chat list shows such a chat with a slowly turning dashed ring instead of the green "done" check, and marks it done only once the agent has picked the result up and gone idle. A chat in the middle of a turn shows the usual working dot even with tasks pending.

- Above the composer, a second line under the activity line reads "Waiting on N background tasks" with how long the oldest has run. Pressing it lists each task's description and how long it has run.

- The chat reads the agent's pending tasks itself, so the status flips as soon as a task is recorded or its report lands, and `/api/agents` reports `is_busy` and `background_tasks`.

- Switching a chat's agent carries its pending commands to the new agent, whose status starts out waiting on them; the switch dialog warns when the chat is busy, since switching stops the old agent.

- A busy chat counts as mid-turn for memory prioritization.
