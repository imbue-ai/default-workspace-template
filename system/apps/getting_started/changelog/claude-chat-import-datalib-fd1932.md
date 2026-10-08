A "Bring in your chats" card at the top of Getting Started.

- The card offers to copy the user's Claude and ChatGPT conversations into the workspace. "Import my chats" starts a chat that asks for the import, which the new `import-chats` skill picks up; "Not now" puts the card away for good.

- While an import runs the card counts each source up, polling `GET /api/chat-import`, which reads the skill's status file. It then shows what came in with a way to check for new chats, or says which source needs the user (a lapsed sign-in, an import that did not finish) with a way to pick it up again. An import whose process is gone reads as unfinished rather than running forever.
