A new chat's first message is no longer lost when Claude is still starting.

- A message held while its agent comes up used to get one try. That covers a new chat's first message, messages sent during a provider switch, and the ones a cancelled switch hands back. If mngr refused it, the chat app only logged the refusal: the agent never saw the message and the page kept showing the agent as thinking. A common case is Claude taking longer than mngr's 30-second readiness window to draw its input box on a first start.

- A refusal that typed nothing and can clear by itself (a blocked input, a harness not ready yet, a host too loaded to answer) is now tried again after 5, 10, 20 and 30 seconds. A send the agent may already hold, such as one a dialog blocked after Claude took it, is still never sent twice.

- A held message that still doesn't reach the agent is reported to the page that sent it, as a new `held_send_failed` socket message. That page takes the message's bubble down, puts its words back in the composer and shows "Couldn't send your message" with the reason, the same way a refused send is shown.
