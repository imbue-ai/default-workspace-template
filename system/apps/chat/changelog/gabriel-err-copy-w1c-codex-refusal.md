The page of a chat whose create failed:

- A Codex chat whose plugin installer refused to install (it found no folder of the agent's own, or no Codex config yet) says "This chat's agent couldn't be set up. Try again, or switch to another provider." in place of "The agent for this chat couldn't be started." The chat app tells it from the installer's `codex-plugin-install-refused` marker in what `mngr create` printed, and records it on the chat as its `failure_kind`. A chat on another harness is never read this way.

- "Report a bug" on the page files the page's sentence with what mngr printed. It stands in for "Ask an agent to fix this" where the page has no agent to ask (no other agent chat exists), and sits under an ask refused with a failure that owes help, beside "Draft it in a chat", carrying the refusal too. A failed chat that names no account offers neither, since a sign-in is what it needs.
