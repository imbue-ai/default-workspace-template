An ask from an app's page ("Ask an agent to fix this") is sent to the chat about that app.

- New `app-ask` launch path and `app_ask` intake target: the ask, led by "This is about the <App> app." so the agent can tell which app it is about, is sent as a message to the chat about the app (the `app_chat` label, else the "<App> Chat" title), held for that chat's page while its create is running (sent if the agent is up when the page takes it, else left in the composer), and creates it with the ask as its first message when there is none and an account is signed in. The main chat takes it instead when that chat is a room, is awaiting its first send or failed to create, nothing is signed in, or the create is refused.

- The chat list passes a chat page's `shell:ask-agent` and `shell:start-with-text` up to the desktop, and hands the page the desktop's answers to both.
