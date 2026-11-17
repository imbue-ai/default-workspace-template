Chat failures without a reason of their own use the workspace's shared sentences instead of "request failed (HTTP 503)", "could not reach the workspace", "unknown error", "The chat list could not open a new chat (503)" or "The chat app answered HTTP 500.".

- A transcript or side conversation that does not load shows its reason, or "The workspace didn't answer. Try again in a moment.", without an "Error:" prefix; the model list in the switch dialog says "Couldn't load the list of models." (or that the workspace didn't answer) with the reason behind "See details".

- A send that failed for a reason the composer has no sentence of its own for says your message wasn't delivered. The composer's failure notice keeps the chat app's reason behind "See details" for every kind, including the ones it words itself.

- A declined `/compact`, and a send refused because the chat's account was signed out, carry their reason as a sentence written for people, so the composer shows it as written rather than only saying the message wasn't delivered. The signed-out notice has "See details" too, holding the status.

- The chat list passes a chat page's "Ask an agent to fix this" (`shell:start-new-with-text`), `shell:start-with-text` and "Report a bug" (`shell:report-bug`) up to the desktop, and hands it the desktop's word on whether a bug report can open, so a chat's failures can offer both.

- Creating a chat, the empty chat list's new chat, reading or applying a message sent from the launcher, and the secret card's submit and decline now read failures through the shared reader.
