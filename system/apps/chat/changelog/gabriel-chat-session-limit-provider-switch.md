The "Sign in again" and "switch to another provider" links now sit under every provider failure in a chat, not just the ones recognised as credential problems.

Hitting a Claude subscription's five-hour limit produced a bare red box with no way out of it. The limit is not an authentication failure and does not read as one: Claude Code stamps it as an ordinary 429, and its wording ("You've hit your session limit - resets 3pm") names no status and no error. It is the same dead end as an expired token, though -- nothing clears it but different credentials -- and the actions were offered only to the auth family, so the most common way to lose a provider mid-conversation was the one way out that offered nothing.

Both links are now offered on any failed turn, including rate limits and overloaded servers. Offering one that turns out to be transient costs nothing: they are inline links that wait to be clicked, not a sign-in screen thrown over the conversation, and switching moves this chat rather than abandoning it. The sentence above them ("This provider is no longer working.") is gone, since the failure's own text already says what happened and that sentence is wrong for a server that is overloaded for the next minute.

A message sent while a chat was switching accounts is no longer lost when the agent it moved to refuses it.

Sending during a switch is answered as accepted and the message is held for the agent the chat is moving to. If that agent then refused it -- which is what happens when the account being switched to is itself out of usage -- the refusal was written to a log and the message was dropped, with nothing in front of the user to say so. The app was holding the only copy.

It now waits on the chat, and the composer takes it back on the next update, above whatever has been typed since. The chat's record survives for it, which for a chat on its first agent it previously did not.
