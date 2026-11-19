# Memory: the agent's notes about the user, in one app

Status: implemented (2026-11-19) with the chat's memory change cards.
Audience: implementers of the Memory app (`system/apps/memory`), the `agent_memory` library (`system/libs/agent_memory`), the chat's memory change cards, and Claude's change-log hook (`system/scripts/agent_memory_change_log.py`).

The agent keeps notes about the user across chats in its memory folder: one Markdown file per note and an index, `MEMORY.md`, that Claude Code loads at the start of every session.
The Memory app lets the user read and edit those notes; the chat shows each change an agent made to them as a card the user can undo.

## 1. Background

- The memory folder is `data/memories/` (Claude's `autoMemoryDirectory`, `.claude/settings.json`).
  A note is a file with YAML frontmatter (`name`, `description`, `metadata.type`) and a body; Claude Code may add bookkeeping keys of its own to the frontmatter.
- The index is loaded into every session as plain text, and lines past a cut-off are dropped. Claude Code's instructions ask for one line per note, under about 150 characters, of the form `- [Title](file.md) — hook`, and forbid writing note content into the index itself.
- Recall also scans the folder directly and uses each file's `description` to decide what is relevant, so a note missing from the index is still found; a note with no description rarely is.

## 2. Decisions

1. The app stores nothing of its own. The notes are read and written through `agent_memory.notes` (`NoteStore`), and the change records through `agent_memory.changes` (`ChangeLog`), the same library the chat's cards and the change-log hook use, so there is one reading of the folder and one Undo.
2. The navigation mirrors the index line by line: its headings, each note a line points at, a line written straight into the index (a "loose" note: saving it in the editor gives it a file of its own and leaves a pointer line in its place), a line pointing at a file that does not exist, and, under "Not in the index", the notes no line points at.
3. A save names the version of the note the editor loaded (a hash of its saved text). A note changed elsewhere since (by an agent, or another window) is refused with `409` and the page offers a reload, so an edit never silently overwrites another.
4. A new title renames the note's file and every `[[link]]` to it. A title may not hold square brackets (they would break the index line's link). An index line longer than `INDEX_LINE_LIMIT` (150 characters) is flagged in the list and under the summary field, and still saves.
5. Each note and the index can be shown as the raw file, read-only; Escape or the toolbar button goes back.
6. A save or delete made in the app moves every chat's baseline past it (`ChangeLog.absorb_user_edit`), so it is never shown as a change the agent made.
7. The agent's changes are shown and undone on the chat's cards, not in the app's page. The app's `/api/changes` routes read and undo the same records for an agent or a script. An Undo puts back the note and its own index lines as they were before the change, and is refused with `409` when one of them changed again since, unless forced.
8. The page reports the note it shows as `/?note=<id>` with the note's title, so the shell reopens the window on it and a chat card's link to that note raises the window already showing it.
9. The page is a plain HTML page on the page kit (`docs/system/themeable-apps.md`, `theming.mode = "tokens"`), with no build step, like System Monitor's.

## 3. Interfaces

- Port 8070, the `memory-app` package; `MEMORY_APP_HOST` and `MEMORY_APP_PORT` move it, and `AGENT_MEMORY_DIR` and `AGENT_MEMORY_CHANGES_DIR` point it at other folders, which a preview sets to copies of both (`[preview]` in `app.toml`).
- The routes and their models are the app's API document at `/api/schema`.
