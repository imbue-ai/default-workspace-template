New `import-chats` skill: bring the user's Claude and ChatGPT conversations into the workspace with datalib.

- The agent connects each account through the usual permission request: Claude through the built-in `claude-ai` connection, ChatGPT through a custom connection that signs in at chatgpt.com and keeps the access token its page fetches (the same sign-in the datalib app uses). After the user approves and signs in, everything else happens without them.

- A script does the import itself: on first use it installs the pinned datalib release (v0.40.0, sha256-checked, plus its runtime), writes datalib's config, and runs one incremental sync in the background. It records each source's progress for the Getting Started card. Each conversation lands as a markdown page under `data/.skills/datalib/`, beside the raw records the sync kept.

- After each sync the script rewrites one index per source (`data/.skills/import-chats/claude-chats.md`, `chatgpt-chats.md`): every conversation by title, newest first and grouped by month, Claude projects in their own section, each linked to its page in the workspace and to the original. datalib names the pages by id, so this is the way to browse them, and the agent's first stop when looking for a past chat.

- `find-transcripts` now points at `import-chats` for Claude and ChatGPT chats instead of saying they cannot be reached.
