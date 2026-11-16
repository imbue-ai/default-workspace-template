- `AGENTS.md` tells a chat to start its background test gate last, once the change is on the user's screen, and to reply without waiting for it: the gate's report wakes the agent, and what it finds is a follow-up fix. Chats were holding the reply for the gate, or ending the turn on "running final checks".

- `AGENTS.md` states the rule the pipe guard enforces (no pipe of a command's output into `head` or `tail`, with the two ways around it), so an agent no longer learns it from a rejected tool call.
