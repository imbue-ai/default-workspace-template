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

- who wrote it and how many other chats have read it, from the chats' own transcripts: a `Write`/`Edit`/`Read` (or
  pi's `write`/`edit`/`read`) tool call on the note's file. Claude's transcripts are read from every project, since a
  worker runs in a worktree of its own, and mapped to an agent through `claude_session_id_history`; pi's sit in each
  agent's own folder (`agents/<id>/plugin/pi_coding/sessions/`). The agent maps to its chat through the chat app's
  `GET /api/chats`. A writer is a live chat (named), an agent no live chat holds (a deleted chat, or a background
  task), or unknown (the chat app could not be asked, or the session belongs to no agent mngr knows); a note no
  transcript explains falls back to the harness its `source` field names ("Saved by a pi chat");
- the file exactly as it is on disk;
- Edit, which rewrites the note's summary (as a double-quoted YAML string, so any text stays one value) and body,
  and its `MEMORY.md` line's summary to match. The edit is made against the version of the note it started from: if
  a chat changes the note meanwhile, the editor shows the chat's version beside the draft, and only "Replace with my
  version" overwrites it;
- Delete, which erases the note's file and drops its lines from `MEMORY.md`. Nothing in the workspace keeps a copy,
  so it cannot be undone; it asks first, and says what still holds the note afterwards: the workspace's backups
  (restic snapshots of the whole home tree, kept for as long as `data/system/backup.toml`'s retention says, read
  through `host_backup.config`), open chats until they restart, and the transcript of the chat that wrote it. The
  "Who can see these notes" panel states the backups' retention up front too.

Every delete and edit is also recorded in `data/.apps/memories/user-changes.jsonl` (the note's file name, what was
done and when; never what it said; kept 30 days). `system/scripts/agent_memory_context.py` turns that record into a
notice chats read -- Claude through a UserPromptSubmit hook in `.claude/settings.json`, pi through its memory
extension -- because an open chat still has the note in its conversation and would otherwise write a deleted note
back, or revert an edit, the next time it saves. The notice is an instruction, not a lock.

Every write checks that the note is still the version given and refuses (409) if a chat changed it meanwhile (a
note's version is read before its text, so a write between the two can only make a save fail), and goes through a
temporary file and a rename. A request body that is not the expected JSON object is refused (400) before anything is
written. A note file that cannot be read is named on the page rather than silently left out. An open Claude chat
loaded the index when it started, and learns of notes saved since through its hook; a pi chat reads the index on
every message.

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
