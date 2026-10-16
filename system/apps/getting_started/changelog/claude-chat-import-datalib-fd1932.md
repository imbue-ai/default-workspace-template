A "Bring in your chats" card at the top of Getting Started.

- The card offers to copy the user's Claude and ChatGPT conversations into the workspace. "Import my chats" starts a chat that asks for the import, which the new `import-chats` skill picks up; "Not now" puts the card away for good.

- While an import runs the card follows it, polling `GET /api/chat-import`, which reads the skill's status file: "ChatGPT: 166 of 582" with a progress bar for a source that reports how much it has to fetch, and a running page count for one that does not. It then shows what came in with a way to check for new chats, or says which source needs the user (a lapsed sign-in, an import that did not finish) with a way to pick it up again. An import whose process is gone, or whose record has stopped being refreshed (the sync rewrites it every ten seconds), reads as unfinished rather than running forever, including after a container restart reuses its process id.
