Chat failures without a reason of their own use the workspace's shared sentences instead of "request failed (HTTP 503)", "could not reach the workspace", "unknown error", "The chat list could not open a new chat (503)" or "The chat app answered HTTP 500.".

- A transcript or side conversation that does not load shows its reason, or "The workspace didn't answer. Try again in a moment.", without an "Error:" prefix; the model list in the switch dialog says "Couldn't load the list of models." (or that the workspace didn't answer) with the reason behind "See details".

- A send the chat refused says your message wasn't delivered, with the reason behind "See details"; a send that got no answer says it may not have been sent, and offers no Retry, which could send it twice. The text is back in the box either way.

- A declined `/compact`, and a send mngr refused because the agent's input is blocked or the agent cannot be reached, carry their reason as a sentence written for people, so the composer shows it as written rather than asking for a bug report.

- The chat list passes a chat page's "Ask an agent to fix this" (`shell:start-with-text`) and "Report a bug" (`shell:report-bug`) up to the desktop, and hands it the desktop's word on whether a bug report can open, so a chat's failures can offer both.

- Creating a chat, the empty chat list's new chat, reading or applying a message sent from the launcher, and the secret card's submit and decline now read failures through the shared reader.
