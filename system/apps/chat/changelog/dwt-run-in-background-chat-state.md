A chat whose agent ended its turn to wait on a background task no longer looks finished.

- The chat list shows such a chat with a slowly turning dashed ring instead of the green "done" check, and marks it done only once the agent has picked the result up and gone idle. A chat in the middle of a turn shows the usual working dot even with tasks pending.

- Above the composer, a second line under the activity line reads "Waiting on N background tasks" with how long the oldest has run. Pressing it lists each task's description and how long it has run.

- The switch dialog warns that the agent wraps up what it is doing when the chat is waiting on a background task, since switching stops the agent and its pending commands.
