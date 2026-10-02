New built-in app, "What agents know": shows what the workspace's Claude chats have written down about you and your work, and lets you correct or remove it.

- It says up front who can see the notes: every Claude chat in this workspace shares them, other chat types (Codex, Pi, OpenCode, Antigravity) don't use them, and other workspaces have their own.

- Each note shows which chat wrote it (or, when no live chat did, that it was a deleted chat or background task, or that the name couldn't be read) and how many other chats have read it, taken from the chats' own records, and the file exactly as it is on disk. A note that can't be read is named rather than left out.

- Edit corrects a note and its line in the index every chat loads. If a chat changes the note while you edit it, the editor shows the chat's version beside yours and only replaces it when you choose to. Delete erases a note for good: there is no "bring back", since nothing in the workspace keeps a copy, and it never deletes a note a chat changed in the meantime.

- Before deleting, it says what still holds the note: the workspace's backups until they expire (it reads the workspace's actual retention, 24 months by default), chats that are open until they restart, and the history of the chat that wrote it. The "Who can see these notes" panel lists the backups too, so this is clear before anything is deleted.

- It runs only while a window shows it.
