Phase 5 of the chat-agent split (`docs/system/blueprint/chat-agent-split/`): a chat can be moved to another harness from its own page.

- In the composer's provider menu, pressing an account on another harness makes it the chat's pending lane: the row wears a "next" badge, the Provider row reads "Claude Code, next: OpenAI (Codex)", and pressing it again takes the choice back. An account on the chat's own harness still offers to open a new chat on it, until a later phase lets a chat change account in place.

- With a lane pending, the send button reads "Switch and send" and asks a two-line confirm ("Claude wraps up what it is doing and stops." then "The conversation continues on OpenAI (Codex), starting with your message."). Confirming starts the handoff with the typed message as the new agent's first; the queued text taken off the old agent comes back to the composer.

- While the chat switches, the held messages stay on the page as bubbles captioned with the phase ("Wrapping up with Claude...", "Claude is writing a summary...", "Starting Codex..."), the strip above the composer says the same, the placeholder says a message typed now is delivered once the new harness is ready, and the Stop button becomes "Cancel switch" until the old agent is stopped. A cancel puts the confirming message back in the composer and keeps the pending lane.

- A switch whose create failed shows "Could not start Codex" with mngr's reason over the composer and a retry on any signed-in account; a refused retry says why.

- The `chats_updated` snapshot's `handoff` object gains `target_harness` and `held_sends` (the confirming message first), so a reloaded page keeps showing what is held; the 409 the app answers to stop, start, rename, interrupt, the queue actions, and the model change while a chat converges now names the harness and the phase in plain words, and the page and the shell show it as is.

- The model bar follows the new agent's harness and model; an optimistic model pick made for the old agent is forgotten when the chat moves.
