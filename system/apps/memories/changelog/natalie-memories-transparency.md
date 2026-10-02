The memories app is now called Agent Memory, and it explains what gets remembered, what chats use, and where the notes go, briefly by default and in full on request.

- The top of the page is the headline, one sentence, and three facts: shared with your Claude and pi chats, not shared with other workspaces, and how long backups keep deleted notes. "How memory works" opens the rest: what chats are told to save and never to save; what chats use (the one-line summaries in `MEMORY.md`, of which chats load the first 200 lines or 25KB); and where notes go (stored in this workspace and its backups, not synced to GitHub, read by Claude and pi chats, sent to a chat's AI provider: the summaries with every chat, a note's full text when a chat opens it).

- Each kind of note says what belongs in it, and an empty kind suggests something to tell a chat.

- Each note is a short card: its summary, who saved it and when, and Edit/Delete. "Show more" opens the full text (the Why and How lines), and the file behind it. A card warns only when chats won't see the note at the start: it isn't in the list chats load (editing the note adds it back) or is past what they load. A warning at the top appears only when something in that list is wrong, including lines that name notes which no longer exist.

- Text and chips meet WCAG AA contrast (4.5:1): headings and hints use the secondary gray, the green chips the accent color, and the amber backups chips dark text.
