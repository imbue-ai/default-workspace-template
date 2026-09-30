New chats start instantly. The chat app now keeps a spare agent already running on the account a new chat would use (one by default; the pool size is one constant), hidden from the chat list, and hands one to each new chat instead of starting an agent then. The chat appears at once, already running, so its first message no longer waits on "Connecting...".

- A spare counts as ready only once its harness accepts input. A new chat that arrives while a spare is still starting takes that spare, rather than starting a create that would queue behind it.

- Signing in to a provider starts the spare immediately, and a new one is started after each hand-over. With a larger pool, the refill waits while another spare is still ready so it does not slow the new chat's first turn.

- The spares follow changes to the default account, project and fast-mode setting, are replaced if their process dies, and survive a restart of the chat app. Chats created with their own name, labels, a pre-minted chat id, or a waived installation check start the way they did.

- Spares never appear in any chat list until a chat takes one, including in a chat preview (a second copy of the chat app beside the live one), which follows the live chat's spares without creating, handing over or destroying any.

- Under memory pressure a spare is shed before any agent or agent subprocess (the new `SPARE_AGENT` band, the ceiling it shares with the browser's renderers), since losing it loses no work. The moment a chat takes it, it moves into the chat band as a freshly started chat, however long it waited, and so do the processes its harness started while it waited. A spare that dies is replaced only after the five-minute backoff, so memory pressure does not churn it.
