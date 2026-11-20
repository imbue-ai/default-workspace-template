The page of a chat whose create failed:

- A Codex chat whose plugin installer refused to install (it found no folder of the agent's own, or no Codex config yet) says "This chat's agent couldn't be set up. Try again, or switch to another provider." in place of "The agent for this chat couldn't be started." The chat app tells it from the installer's `codex-plugin-install-refused` marker in what `mngr create` printed, and records it on the chat as its `failure_kind`. A chat on another harness is never read this way.

- Every failed create offers "Report a bug", after "Ask an agent to fix this" where the page offers one, which files the page's sentence with what mngr printed. When "Ask an agent to fix this" was refused, the same report carries the refusal too, and the refusal has no "Report a bug" of its own.
