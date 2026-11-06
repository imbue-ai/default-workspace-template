A message whose send got no answer from the chat app is no longer reported as failed while it may have arrived, or delivered twice.

- A message whose send got no answer from the chat app (the connection dropped, or a proxy gave up on a long send) is no longer reported as failed while it may have been delivered. The page sends it again under the same send-time id for about half a minute, and the chat app answers a repeat as it answered the first send, a refusal included, waiting for it while it is still under way, instead of delivering it twice. If it still gets no answer, it gets the unconfirmed notice ("Couldn't confirm your message arrived", Send again, no Force), and its copy goes back in the composer and comes back out once the agent's turn for it shows up. Retry after a failed send goes under the failed send's id the same way.

A message is no longer lost or shown twice when the chat's sign-in fails.

- When a chat's login expires and the user signs that account in again, the chat sends again, on its own, the message the expired login refused, once the sign-in has restarted the agent. Every live chat on that account whose last reply was the sign-in error does this, wherever the sign-in finished. While it happens, the failed turn reads "Signed in again. Sending your message again…". Nothing is sent again for a turn the agent had already started working on, when the user has written since, or while the chat is switching to another provider.

- When the chat cannot send it on its own (the chat was stopped, its restart or the resend failed), the failed turn reads "Signed in again." and offers "Send your message again" while it is still the chat's newest turn. When a resend of several refused messages stops partway, the ones it did not get to stay offered. The provider's original text stays under "See details". Each account now records when it last finished a sign-in, and `GET /api/accounts` carries it as `signed_in_at`.

- "Send your message again" under a sign-in error handles a send as the composer does: one the chat app delivered by sending it twice shows "Sent again" under the message, one that cannot be confirmed or gets no answer says so and offers Send again in place of Try again, and every press sends the message under the id its first press used.

- Once a refused message is sent again right after its sign-in error, by the chat, by the button, or typed again by hand before anything else, the original message and its sign-in error are hidden, so the message shows once, whichever order the agent wrote the error and the message in. The same words written after some other message are a new message, and hide nothing. This replaces the rule that hid every sign-in error from before the chat's first reply: such an error now stays when the user wrote something else next.

The failure notice for a send now says what happened in the chat's own words for each kind of failure, rather than showing mngr's reason, and the Force button is now Restart. An unclassified failure still shows the reason, the only account of what went wrong.

Stopping an agent while a message is still being sent to it no longer leaves two copies in the composer and a failure notice: the message comes back once.

The chat app's log now records each failed send's kind and each message it sent again.

"Sent again" now comes from the chat app rather than the page that sent the message, so it shows in every window and after a reload. It also covers a message held through a chat switch and an Antigravity block that lands on a later pass, which showed nothing before. The chat app forgets these when it restarts.
