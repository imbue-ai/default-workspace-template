A new chat opens as a blank, ready chat: no "Starting the chat...", "Loading events..." or "No events yet for this agent." placeholder at any point. While the agent is created, while its transcript loads, and once it is up with nothing in it, the page is the transcript's own empty list with the composer ready, so nothing changes on screen as the chat comes up.

A message sent while a new chat is still starting stays where it was typed instead of sitting at the bottom of the screen and jumping to the top once the chat came up.

New chats are no longer greeted: a chat started with nothing to say is created without a first message and waits for the user's, so a message sent during startup is no longer queued behind a `/welcome` turn. Older chats' transcripts still hide the `/welcome` turn they began with.
