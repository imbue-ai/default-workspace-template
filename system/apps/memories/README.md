# memories

The memories app ("What agents know" on the desktop): what the workspace's Claude chats have written down about the
user and their work, on one page a window of the desktop frames at the app's own origin.

Claude's built-in memory keeps one Markdown note per thing it learned in `data/memories/` (`autoMemoryDirectory` in
`.claude/settings.json`), plus `MEMORY.md`, an index every Claude chat loads when it starts. Every Claude chat in the
workspace shares the folder; other harnesses (Codex, Pi, OpenCode, Antigravity) do not use it, and other workspaces
have their own. The page says so before anything else, then shows each note grouped by its `type` (about you, how you
like things done, what you're working on, where things are), with:

- who wrote it and how many chats have read it, from the chats' own transcripts (a `Write`/`Edit`/`Read` tool call
  on the note's file, mapped to its agent through `claude_session_id_history` and to its chat through the chat app's
  `GET /api/chats`);
- the file exactly as it is on disk;
- Edit, which rewrites the note's summary and body and its `MEMORY.md` line's summary to match;
- Forget, which moves the note to `data/.state/memories/forgotten/<id>/` beside a record of the index lines it had,
  so no chat finds it, and drops those lines from `MEMORY.md`; Bring back puts both back.

Every write checks that the note is still the version the page read and refuses (409) if a chat changed it
meanwhile, and goes through a temporary file and a rename. A chat that is already open has loaded the index, so it
sees a change when it next starts.

It runs as the `memories` supervisord program (`system/supervisord.conf.d/memories.conf`) from its own uv tool
environment, serving on `http://127.0.0.1:8050`:

- `GET /`, `GET /assets/...`: the page.
- `GET /api/health`: `{"status", "is_frontend_built"}`.
- `GET /api/notes`: the notes, their attribution, and the forgotten notes.
- `PUT /api/notes/<file>`: `{"description", "body", "version"}`.
- `POST /api/notes/<file>/forget`: `{"version"}`.
- `POST /api/forgotten/<id>/restore`.
- `GET /_static/app_contract.js`: the shell's browser-side contract module.

The manifest declares `stop_when_no_windows`: the app holds nothing between requests, so the shell stops it a minute
after its last window closes.
