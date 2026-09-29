New chats start instantly. The chat app now keeps two spare agents already running on the account a new chat would use, hidden from the chat list, and hands one to each new chat instead of starting an agent then. The chat appears at once, already running, so its first message no longer waits on "Connecting...".

- A spare counts as ready only once its harness accepts input. A new chat that arrives while a spare is still starting takes that spare, rather than starting a create that would queue behind it.

- Signing in to a provider starts the spares immediately. The pool is refilled after each hand-over, delayed while another spare is still ready so the refill does not slow the new chat's first turn.

- The spares follow changes to the default account, project and fast-mode setting, are replaced if their process dies, and survive a restart of the chat app. Chats created with their own name, labels or a pre-minted chat id start the way they did.
