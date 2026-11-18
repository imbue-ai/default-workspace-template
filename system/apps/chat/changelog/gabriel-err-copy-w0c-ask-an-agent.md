An ask from an app's page ("Ask an agent to fix this") is sent to the chat about that app.

- New `app-ask` launch path and `app_ask` intake target: the ask is sent as a message to the chat about the app (the `app_chat` label, else the "<App> Chat" title), held for it while its create is running, and creates it with the ask as its first message when there is none and an account is signed in. The main chat takes it instead when that chat is a room, is awaiting its first send or failed to create, nothing is signed in, or the create is refused.

- The chat list passes a chat page's `shell:ask-agent` and `shell:start-with-text` up to the desktop, and hands the page the desktop's answers to both.
