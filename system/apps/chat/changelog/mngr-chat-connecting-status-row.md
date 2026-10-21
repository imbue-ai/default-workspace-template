A chat now says where your message is at every moment from Enter to the reply, is usable while its agent is still starting or connecting, keeps every message sent to a chat that is still starting, and keeps the name you give it.

**Where your message is**

A chat now always says where your message is, from the moment you press Enter until the turn is done. One status row sits at the end of the conversation, where the reply will appear:

- Right after you send, a pulsing dot shows under your message at once. If the message is still on its way after a moment, the row says "Sending to Claude Code…"; while the agent is still coming up it says "Connecting to <model> on <provider>…" (this moved here from beside the model chip).

- While the agent works, the row shows "Thinking…" or what the agent is doing, with how long you have been waiting once it passes a few seconds, counted from when you pressed Enter.

- As soon as the reply is in, a check and "Done in 14s" (how long you waited) sit under it, even while the agent is still tidying up behind the scenes.

Before, nothing on the page moved between sending and the agent starting its turn, or between the reply and the turn ending, and nothing said a turn was done.

**Usable while starting or connecting**

A chat is usable while its agent is still starting or connecting.

- Stop is there as soon as a message is sent, including while the chat connects. Pressing it while the message is still waiting for the agent takes the message back into the composer, unsent; once the agent is up it interrupts as before. This also covers the first message of a new chat.

- A chat that is still starting can be renamed and deleted from the chat list and the phone header. The new name shows at once and the agent takes it when it lands; a deleted chat leaves the list at once and its agent is removed as soon as it exists.

- The provider and model of a chat that is still starting can be changed from its model chip. A new model is applied as the agent comes up; another provider moves the chat to it then, carrying the messages already sent.

- "Connecting…" names what the chat waits for, such as "Connecting to Opus 5.5 on Anthropic…".

- Typing into a chat whose agent is stopped starts the agent in the background, so the message usually finds it up and "Connecting…" is rarely seen.

- The agent's terminal view opens at once for an agent that is already running, instead of saying "Starting agent..." while it checks. It says "Starting agent…" only for a stopped one.

**Starting, switching and renaming show what is happening**

Starting, switching and renaming chats now show what is happening instead of a blank page.

- A new chat's empty page says "Starting your chat…" while the chat is created, then "Your chat is ready." once it is up. The line gives way to the first message.

- Switching to a chat whose page has to load draws the shape of a few turns instead of an empty white page. It fades in only after 400 ms, so a quick switch shows nothing in between.

- Renaming a chat shows the new name in the list, the phone header and the window title as soon as the field closes, instead of after the seconds the rename takes. If the chat app refuses the rename, the old name comes back and the field reopens with the reason.

**Messages sent to a starting chat are kept**

A message sent to a chat that is still starting is no longer lost when that chat's page goes away before the chat is up. The chat app now holds the message itself (answering the send with `202 held`) and delivers it, in order and after any first message the create carried, as soon as the chat's agent lands. Before, the message waited only in the page's memory: the chat list keeps at most four chat pages loaded and drops the oldest, so starting several chats quickly (each typed into while it started) silently dropped the earlier chats' messages, and so did a reload or a closed window.

Every page shows a starting chat's held messages from the chat's record, so a page opened or reloaded while the chat starts still shows them. A create that fails keeps them, shows them above its reason, and its Try again delivers them once the chat starts. Any sender can rely on this, not only a chat page: `POST /api/chats/<id>/message` to a chat whose create is running is held rather than answered 404.

**Chat names**

Chat names are now consistent, and a name you give a chat always sticks.

- A name you type no longer flips back to "Chat N". After a rename, mngr's observe stream sometimes delivered a snapshot it had read before the rename, and the list showed the old name again for several seconds (measured on a staging workspace: back to the old name 0.8 s after the rename landed, for 3.3 s). The chat app now keeps the new name over such a snapshot until the stream reports the rename.

- Your rename wins over the automatic name. Before, the automatic name could be shown over the name you just typed, and its rename could run alongside yours and land last, replacing your name. Now, while your rename runs, no automatic name is shown or made for that chat, and an automatic rename already running finishes before yours. The rail keeps the name you typed until the chat app answers, instead of giving way to the first other name it shows.

- Every chat whose first message says what it is about gets a name, whatever provider it runs on. Chats on Google (Antigravity), OpenCode Go, OpenRouter or an API key used to keep "Chat N" for good, because those providers have no way to ask a small model; so did chats whose account had been signed out, chats where the model call failed, and chats where the model answered the message instead of naming it (a message saying "reply with OK" got "OK"). Those chats are now named from the opening words of the first message, and the message reaches the model marked off as material to name, so it is named rather than answered. An opener with no clear subject ("hi") still keeps "Chat N" and the next message is tried, up to three.
