# memories

The memories app ("Agent Memory" on the desktop): what the workspace's Claude and pi chats have written down about
the user and their work, on one page a window of the desktop frames at the app's own origin.

Claude's built-in memory keeps one Markdown note per thing it learned in `data/memories/` (`autoMemoryDirectory` in
`.claude/settings.json`), plus `MEMORY.md`, an index every Claude chat loads when it starts. pi chats keep the same
notes: `.pi/extensions/memory.ts` gives every pi turn the protocol in `.agents/shared/references/memory-protocol.md`
(adapted from Claude Code's own memory prompt, so both write one format) and the index, through
`system/scripts/agent_memory_context.py`. Codex, OpenCode and Antigravity do not use the folder yet, and other
workspaces have their own. A note pi saves says so in `metadata.source: pi-coding`; Claude Code marks its own with
`originSessionId`. The same script keeps each note's `MEMORY.md` line equal to the note's `description` after any
chat writes to the folder, so the line a chat starts from never contradicts the note.

The top of the page is short: a line on when chats save a note, three facts (shared with Claude and pi chats, not
shared with other workspaces, how long backups keep deleted notes), and a line naming anything wrong with the index
(notes it leaves out, notes past what chats load, lines for notes that no longer exist). "How memory works" opens the
rest: what chats are told to save and never to save, how much of the index chats load (the first 200 lines or 25KB,
the limit Claude Code applies), where the notes go (stored here and in backups, read by Claude and pi chats, sent to a
chat's AI provider when used, not synced to GitHub), and the technical details. Below that, each note is grouped by
its `type` (about you, how you like things done, what you're working on, where things are), with:

- the summary and who saved it, with the full text and the file behind "Show more", so the page reads as a list;
- a warning when chats won't see it at the start: it has no line in `MEMORY.md`, or its line falls past the load
  limit;
- who wrote it and how many other chats have read it, from the chats' own transcripts: a `Write`/`Edit`/`Read` (or
  pi's `write`/`edit`/`read`) tool call on the note's file. Claude's transcripts are read from every project, since a
  worker runs in a worktree of its own, and mapped to an agent through `claude_session_id_history`; pi's sit in each
  agent's own folder (`agents/<id>/plugin/pi_coding/sessions/`). The agent maps to its chat through the chat app's
  `GET /api/chats`. A writer is a live chat (named), an agent no live chat holds (a deleted chat, or a background
  task), or unknown (the chat app could not be asked, or the session belongs to no agent mngr knows); a note no
  transcript explains falls back to the harness its `source` field names ("Saved by a pi chat");
- the file exactly as it is on disk;
- Edit, which rewrites the note's summary (as a double-quoted YAML string, so any text stays one value) and body,
  and its `MEMORY.md` line's summary to match, adding the line when the index has none. The edit is made against the version of the note it started from: if
  a chat changes the note meanwhile, the editor shows the chat's version beside the draft, and only "Replace with my
  version" overwrites it;
- Delete, which erases the note's file and drops its lines from `MEMORY.md`. There is no copy here to restore,
  so it cannot be undone; it asks first, and says what still holds the note afterwards: the workspace's backups
  (restic snapshots of the whole home tree, kept for as long as `data/system/backup.toml`'s retention says, read
  through `host_backup.config`), and the conversations and transcripts of chats that read it (open chats are told
  of the delete; see below). The top of the page states the backups' retention up front too.

"Settings" pauses memory for every chat or turns it off for Claude or pi chats, as the Claude apps' memory settings
do: chats keep what is saved but neither use it nor save anything new. The switches are kept in
`data/.apps/memories/settings.json` (`{"is_paused": false, "disabled_harnesses": []}`; no file means on).
`agent_memory_context.py` reads them before every message: pi gets a short "memory is off" notice in place of its
memory, and Claude's UserPromptSubmit hook says the same on every message (and once that it is back on). Turning
Claude's memory off also sets `autoMemoryEnabled: false` in the workspace's `.claude/settings.local.json`, keeping
that file's other keys, so a new Claude chat neither loads nor saves memory; Claude Code reads it only when a chat
starts, and workers in their own worktrees rely on the notice. A settings file that cannot be read counts as off.

Every delete and edit is also recorded in `data/.apps/memories/user-changes.jsonl` (the note's file name, what was
done and when; never its text, though the file name often summarises it; kept 30 days). `system/scripts/agent_memory_context.py` turns that record into a
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
- `GET /api/notes`: the notes with their attribution and index line, the index's size and missing files
  (`index`), the backups' retention (`backups`), the memory switches (`controls`, null when unreadable), and
  `messages` for anything that could not be read.
- `PUT /api/notes/<file>`: `{"description", "body", "version"}`.
- `DELETE /api/notes/<file>`: `{"version"}`.
- `PUT /api/controls`: `{"is_paused", "disabled_harnesses"}`, the memory switches.
- `GET /_static/app_contract.js`: the shell's browser-side contract module.

The manifest declares `stop_when_no_windows`: the app holds nothing between requests, so the shell stops it a minute
after its last window closes.
