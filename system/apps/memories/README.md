# memories

The memories app ("What agents know" on the desktop): what the workspace's Claude chats have written down about the
user and their work, on one page a window of the desktop frames at the app's own origin.

Claude's built-in memory keeps one Markdown note per thing it learned in `data/memories/` (`autoMemoryDirectory` in
`.claude/settings.json`), plus `MEMORY.md`, an index every Claude chat loads when it starts. Every Claude chat in the
workspace shares the folder; other harnesses (Codex, Pi, OpenCode, Antigravity) do not use it, and other workspaces
have their own. The page says so before anything else, then shows each note grouped by its `type` (about you, how you
like things done, what you're working on, where things are), with:

- who wrote it and how many other chats have read it, from the chats' own transcripts (a `Write`/`Edit`/`Read` tool
  call on the note's file, in any project's transcripts since a worker runs in a worktree of its own, mapped to its
  agent through `claude_session_id_history` and to its chat through the chat app's `GET /api/chats`). A writer is a
  live chat (named), an agent no live chat holds (a deleted chat, or a background task), or unknown (the chat app
  could not be asked, or the session belongs to no agent mngr knows);
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

Every write checks that the note is still the version given and refuses (409) if a chat changed it meanwhile (a
note's version is read before its text, so a write between the two can only make a save fail), and goes through a
temporary file and a rename. A request body that is not the expected JSON object is refused (400) before anything is
written. A note file that cannot be read is named on the page rather than silently left out. A chat that is already
open has loaded the index, so it sees a change when it next starts.

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
