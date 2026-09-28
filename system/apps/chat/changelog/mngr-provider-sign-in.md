- Signing in to Claude or ChatGPT no longer needs a pasted code when the chat runs inside the minds desktop app. Picking Claude or ChatGPT hands the sign-in page to the desktop app, which opens it in your browser and relays the provider's callback back into the workspace; the chooser shows "Finish in your browser" (with "Open it again" and Cancel) and closes by itself once you are signed in. Without the desktop app, Claude falls back to pasting the code its page shows, and ChatGPT to its one-time-code login. The top row's sign-in starts as the chooser opens, so it is ready by the time you click.

- ChatGPT signs in through a short-lived Codex app-server rather than by scraping `codex login --device-auth`: "Use your ChatGPT plan (runs on Codex)" in the browser, or "ChatGPT with a code" from any device.

- New `POST /api/accounts/flow/<flow_id>/callback` replays a relayed browser callback against the sign-in CLI: the first request must be the callback with the flow's `state`, then other requests on that port go through for 60 seconds.

- Only the workspace's owner can connect, change or remove an AI account: those routes answer 403 "Only the owner of this workspace can connect an AI account." to a visitor. Reading the accounts stays open, since a visitor's chats render from them.

- A pasted Anthropic or OpenAI key is checked with its provider before it is saved: a rejected key says "That key was rejected by <provider>." and saves nothing, and a provider that cannot be reached saves the key and says "Couldn't check this key".

- Signing in again no longer takes the account's old credential away first; the account keeps working until the new sign-in lands. A Claude sign-in is decided by the CLI's own "Login successful" and a clean exit.

- Claude subscription tokens are no longer accepted or kept: the long-lived-token sign-in is gone, a pasted `sk-ant-oat01-` token or `CLAUDE_CODE_OAUTH_TOKEN` line is refused, and an account still running on one offers "Switch to a normal sign-in", which signs it in again and drops the token.
