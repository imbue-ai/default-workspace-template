New `SPARE_AGENT` band (1000, the ceiling) for a spare chat agent: one the chat app starts ahead of the next new chat and that no one uses yet. It is shed before any other agent or agent subprocess under memory pressure. Once a chat takes a spare, the chat band policy holds that chat at the engaged floor for its first minute (`CHAT_JUST_STARTED_GRACE_SECONDS`) before scoring it like any chat.

`memory_candidates.py` leaves the chat app's spare agents out of its idle chats, since no chat list shows a spare until a chat takes it.
