# memories

The memories app ("What agents know" on the desktop): what the workspace's Claude chats have written down about the
user and their work, on one page a window of the desktop frames at the app's own origin.

Claude's built-in memory keeps one Markdown note per thing it learned in `data/memories/` (`autoMemoryDirectory` in
`.claude/settings.json`), plus `MEMORY.md`, an index every Claude chat loads when it starts. pi chats keep the same
notes: `.pi/extensions/memory.ts` gives every pi turn the protocol in `.agents/shared/references/memory-protocol.md`
(adapted from Claude Code's own memory prompt, so both write one format) and the index, through
`system/scripts/agent_memory_context.py`. Codex, OpenCode and Antigravity do not use the folder yet, and other
workspaces have their own. A note pi saves says so in `metadata.source: pi-coding`; Claude Code marks its own with
`originSessionId`. The page says so before anything else, then shows each note grouped by its `type` (about you, how you
like things done, what you're working on, where things are), with:

- for a note pi saved, "Saved by a pi chat"; for Claude's, who wrote it and how many chats have read it, from the chats' own transcripts (a `Write`/`Edit`/`Read` tool call
  on the note's file, mapped to its agent through `claude_session_id_history` and to its chat through the chat app's
  `GET /api/chats`);
- the file exactly as it is on disk;
- Edit, which rewrites the note's summary and body and its `MEMORY.md` line's summary to match;
- Delete, which erases the note's file and drops its lines from `MEMORY.md`. Nothing in the workspace keeps a copy,
  so it cannot be undone; it asks first, and says what still holds the note afterwards: the workspace's backups
  (restic snapshots of the whole home tree, kept for as long as `data/system/backup.toml`'s retention says, read
  through `host_backup.config`), open chats until they restart, and the transcript of the chat that wrote it. The
  "Who can see these notes" panel states the backups' retention up front too.

Every delete and edit is also recorded in `data/.state/memories/user-changes.jsonl` (the note's file name, what was
done and when; never what it said; kept 30 days). `system/scripts/agent_memory_context.py` turns that record into a
notice every chat reads before each message -- Claude through a UserPromptSubmit hook in `.claude/settings.json`, pi
through its memory extension -- because an open chat still has the note in its conversation and would otherwise write
a deleted note back, or revert an edit, the next time it saves. The notice is an instruction, not a lock.

Every write checks that the note is still the version the page read and refuses (409) if a chat changed it
meanwhile, and goes through a temporary file and a rename. A chat that is already open has loaded the index, so it
sees a change when it next starts.

It runs as the `memories` supervisord program (`system/supervisord.conf.d/memories.conf`) from its own uv tool
environment, serving on `http://127.0.0.1:8050`:

- `GET /`, `GET /assets/...`: the page.
- `GET /api/health`: `{"status", "is_frontend_built"}`.
- `GET /api/notes`: the notes, their attribution, and the backups' retention (`backups`).
- `PUT /api/notes/<file>`: `{"description", "body", "version"}`.
- `DELETE /api/notes/<file>`: `{"version"}`.
- `GET /_static/app_contract.js`: the shell's browser-side contract module.

The manifest declares `stop_when_no_windows`: the app holds nothing between requests, so the shell stops it a minute
after its last window closes.
