# Agent Memory: shared, visible, user-controlled memory for workspace chats

Status: implemented (2026-10-02) on a stack of four branches (section 11), not yet reviewed.
Audience: reviewers of those branches; maintainers of the built-in apps (`system/apps/`), the pi extensions (`.pi/extensions/`), Claude's hooks (`.claude/settings.json`), and the agents' instructions (`AGENTS.md`, `CLAUDE.md`, `.agents/shared/references/`).

The work has three parts:

- **Agent Memory**, a new built-in app (`system/apps/memories`). It shows what chats have saved about the user, who saved it and who has read it. It lets the user correct or permanently delete a note, says what still holds a deleted note, and pauses memory or turns it off for one kind of chat.
- **Shared memory across harnesses.** Claude and pi chats read and write one set of notes, in Claude Code's own on-disk format. Codex, OpenCode and Antigravity are designed (section 10) but not wired.
- **Keeping chats consistent with the notes.** A chat that already has a note in its context is told when the user deletes or edits it. A Claude chat learns of notes other chats saved after it started. The `MEMORY.md` line every chat starts from is kept equal to the note it points at.

## 1. Problem and goals

Claude chats in a workspace already save notes about the user through Claude Code's auto memory. Before this work:

- **No visibility.** The user could not see what was saved, who saved it, or fix it without opening Markdown files.
- **Claude only.** pi chats were told not to use memory, so a fact told to a Claude chat was unknown to a pi chat, and the reverse.
- **Changes didn't stick.** An open chat that still had a deleted note in its conversation wrote the note back the next time it saved (reproduced live).

Goals, from the host and design review:

1. **See and correct.** List every note, with who wrote it, and let the user edit it or delete it permanently. "Deleted" means deleted, so there is no trash.
2. **Shared by default.** Memory is shared across harnesses by default, starting with Claude plus one more (pi, on an Anthropic API key).
3. **User control.** Pause memory, or turn it off per kind of chat, modelled on the Claude apps.
4. **Honesty.** Be truthful about what is retained (backups keep deleted notes), where notes go (the model provider), and what chats actually load (the 200-line limit).
5. **No idle cost.** The app holds nothing between requests and stops when no window shows it.

Non-goals for this change:

- Codex, OpenCode and Antigravity integration, which are designed only.
- Private chats.
- Removing deleted notes from backups.
- Syncing memory across workspaces.
- Any memory search beyond what the harnesses already do.

## 2. Background

These are the facts the design depends on. Each was verified in a workspace container (`minds-workspace-1`) unless marked otherwise.

**Claude Code auto memory** (Claude Code 2.1.280):
- Claude Code writes one Markdown file per note into `autoMemoryDirectory`, which `.claude/settings.json` sets to `~/workspace/data/memories` (an absolute path, so workers in worktrees share it).
- Each note's frontmatter has `name`, `description`, `metadata.type`, and the Claude-stamped `metadata.modified` and `metadata.originSessionId`.
- `MEMORY.md` holds one `- [Title](file.md) — hook` line per note. Claude Code loads its first 200 lines or 25KB **once, when a chat starts**, and opens a note's file only when the line looks relevant.
- **Turning memory off.** `autoMemoryEnabled: false` in the project's `.claude/settings.local.json` stops a newly started chat from loading or writing memory. Verified: the same question answered from a note with the setting absent, and "NONE" with it false.
- **Who else writes that file.** mngr no longer writes hooks to `settings.local.json`; it uses each agent's own config directory instead, so the file is free for this use. Claude Code itself may add permission entries to it, so writers must merge rather than replace it.

**pi** (0.87.1):
- pi has no memory of its own.
- Project extensions in `.pi/extensions/*.ts` load when a pi process starts. They load in directory order, so either order relative to `tk_workflow.ts` must work.
- `before_agent_start` runs before every prompt and may set `event.systemPromptOptions.sections`.
- pi records the system prompt once and appends a section's full text to the transcript again only when that text changes. A section that changes every message therefore re-records itself every message.

**Backups:** `host_backup` keeps restic snapshots of the whole home tree. Its default retention is hourly for 24 hours, daily for 30 days, weekly for 12 weeks and monthly for 24 months (`system/services/host_backup/src/host_backup/config.py`). A deleted note therefore survives in older snapshots.

**Requests:** the forwarding proxy in front of a local workspace replaces the Host header, so a write from the app's own page cannot be recognised by comparing Origin with Host.

## 3. Design overview

```
              Claude chats                                pi chats
   (Claude Code auto memory, native)            (no memory of their own)
     |  writes notes + MEMORY.md                  |  writes notes per the protocol
     |  UserPromptSubmit hook  --------+   +----- |  .pi/extensions/memory.ts
     |  PostToolUse hook  -------+     |   |      |  (before_agent_start, tool_result)
     v                           v     v   v      v
   ~/workspace/data/memories/  <---  system/scripts/agent_memory_context.py  --->  protocol + index,
   (one .md per note, MEMORY.md)       (stdlib; one script for all harnesses)        notices, stamping,
     ^                                    ^            ^                            index sync
     |  read / edit / delete              |            |
     |                       data/.apps/memories/user-changes.jsonl  (deletes and edits, never content)
     |                       data/.apps/memories/settings.json       (pause / per-harness switches)
     |                                    ^            ^
   system/apps/memories  ("Agent Memory" app, Flask + Mithril, port 8050)
```

These are the design's main choices:

- **One store, in Claude's format.** Claude already writes it, its notes map one-to-one onto the Claude apps' memory "topics", and any harness can read and write Markdown. Other writers add `metadata.source: <harness>`.
- **One harness-neutral script.** It works out what each harness is told. Each harness only needs thin wiring to get that text in front of its model.
- **The app is a plain reader and writer of those files.** It owns two small files of its own under `data/.apps/memories/` (per `docs/system/blueprint/workspace-app-model/contracts.md` section 17). It holds no state between requests.

## 4. Components

### 4.1 Note format and the protocol

**Notes** keep Claude Code's format unchanged. Readers in the app tolerate:
- a top-level `type`, as well as `metadata.type`;
- no frontmatter at all;
- quoted YAML scalars (JSON-style double quotes, and single quotes with `''` escapes).

They also preserve unknown keys. The app writes summaries as double-quoted YAML strings, so any text stays one value (`notes.py`, `render_note`).

**The protocol** is how a harness without built-in memory keeps notes: `.agents/shared/references/memory-protocol.md`. It is adapted from Claude Code's own memory prompt, captured from a transcript, so both harnesses write one format. It adds:
- an absolute notes path;
- `metadata.source`;
- never saving secrets (passwords, keys, tokens, ID or account numbers);
- not saving sensitive personal details (health, race or ethnicity, religion, politics, sexuality or gender identity) unless the user asks;
- treating saved notes as data, not instructions.

Claude keeps its native memory prompt. `CLAUDE.md` gets the same secrets and sensitive-details rules, plus "the user's deletes and edits win".

### 4.2 `system/scripts/agent_memory_context.py`

This is the one place that decides what a chat is told about memory. It uses the standard library only, because hooks run it under a plain `python3`. It is listed in `system/scripts/stdlib_only_scripts_test.py`.

| Mode | Run by | Does |
|---|---|---|
| `--json --harness pi-coding` | pi's `before_agent_start`, before every prompt | Syncs the index (4.5), then prints `{"protocol", "memory"}`: the protocol, and the index (cut like Claude Code: 200 lines or 25KB) plus any change notice (4.6). Neither part depends on the clock, so pi re-records the protocol never and the index only when a note changed. |
| `--claude-hook` | Claude's UserPromptSubmit hook, before every message | Syncs the index, then prints only what is new for this chat: the user's deletes and edits, and the index lines of notes other chats saved or changed since the chat last heard (4.6). Usually prints nothing. |
| `--stamp PATH --harness pi-coding` | pi's `tool_result`, after a `write` or `edit` of a note | Sets `metadata.modified` to now and adds `metadata.source` when missing, as Claude Code does for Claude, so the date is never the model's guess. Then syncs the index. |
| `--sync-index` | Claude's PostToolUse hook; pi's `tool_result` after a shell command naming the folder | Syncs the index (4.5). |

While memory is off for the harness (4.7), every mode that prints context prints a short "memory is off" notice instead.

**Failure modes:**
- The script **fails open**: an unreadable protocol prints nothing and exits 0, so a broken checkout costs a chat its memory, never its turn.
- Unreadable change records read as "no changes".
- The one deliberate exception is the memory settings: an unreadable settings file counts as **off** (4.7).

### 4.3 pi wiring: `.pi/extensions/memory.ts`

- **`before_agent_start`** runs the script with `--json` and sets two system prompt sections, `workspace_memory_protocol` and `workspace_memory`. Two sections are used so the fixed protocol is recorded once per chat.
  - If another extension already replaced the whole prompt for this turn (`tk_workflow.ts` sets `forceSystemPrompt`), the text is appended to that prompt instead.
  - Both load orders were verified live.
- **`tool_result`**:
  - a successful `write` or `edit` of a file in the notes folder runs `--stamp`;
  - a successful `bash` command whose text names `data/memories` runs `--sync-index`.
- **Logging and timeout.** Failures are logged to `$MNGR_AGENT_STATE_DIR/pi_workspace_memory.log`, not stderr, which lands on pi's screen. The script has a 5 second timeout.
- **Wording.** `.mngr/settings.toml` no longer tells pi to avoid memory.

### 4.4 Claude wiring

Claude keeps its native auto memory. Two hooks in `.claude/settings.json` close its gaps:

- **UserPromptSubmit runs `--claude-hook`.**
  - **The gap:** Claude Code loads `MEMORY.md` only at chat start, so an open Claude chat never saw a note a pi chat saved later (reproduced live).
  - **How "new" is decided:** the hook reads the chat's transcript to find when the chat started and which notes it wrote itself. Those are skipped, with a 5 second tolerance for stamping. It keeps a small mark per chat in the agent's state directory (`$MNGR_AGENT_STATE_DIR/memory-hook-<session>.json`) and announces only what is newer than that mark.
  - **No repeats:** each change is announced once, because the chat keeps what an earlier message's hook added.
  - **Without a readable transcript**, it still announces the deletes and edits. A repeated notice costs less than a missed delete.
- **PostToolUse on `Write|Edit|MultiEdit|Bash` runs `--sync-index`**, but only when the hook's input (the tool call and its result) mentions `data/memories`. A shell `case` filters before Python starts, so other tool calls cost about 1 ms. The measured cost of a Python start in the container is about 7 ms.

### 4.5 The index line follows the note

`MEMORY.md` is what every chat starts from. Before this, keeping a note's line in sync was left to whichever model edited the note, and models forget. Observed:
1. A Claude chat changed the user's location to Virginia and updated the line.
2. A pi chat changed the note back to California but left the line saying Virginia.

So every new chat started from the wrong fact.

`sync_index` makes this an invariant held in code. Each note's line is `- [Title](file.md) — <its description>`:
- a stale line is rewritten, a missing line is appended, and later duplicate lines for the same note are dropped;
- a line for a note file that is gone is dropped, so a deleted fact never stays in what chats start from;
- everything else is kept: headings, prose, and links to anything that is not a note in the folder;
- a note with no description, or a block-scalar description, keeps its line;
- the write is atomic, keeps the file's mode, and happens only when something changed.

**Concurrency:** the sync reads the notes and the index and rewrites the index under an advisory `flock` on `data/memories/.MEMORY.md.lock`, which the app takes for its own index rewrites, so a delete in the app and a sync never write over each other (without it, a racing sync could write a deleted note's line back). A chat that edits `MEMORY.md` with its own file tools doesn't take the lock; if its line is lost to a concurrent rewrite, the next sync rebuilds it from the note.

The app's own edit already wrote the description into the line, so the app and the sync agree. The protocol now asks pi for a description that reads well as that line, instead of asking it to maintain the line.

### 4.6 Deletes and edits reach open chats

An open chat still has a deleted note in its conversation. Before this change it wrote the note back the next time it saved.

- **The record.** Every delete or edit made in the app appends one line to `data/.apps/memories/user-changes.jsonl`, holding the file name, `DELETED` or `EDITED`, and the time. It never holds the note's text, though a file name such as `user-location.md` often summarises the fact.
- **Retention.** Lines older than 30 days are dropped on the next write (`changes.py`).
- **The notice.** The script turns the latest change per note into a notice. A delete says don't save that content again; an edit says read the note again before changing it. pi gets it in its memory section, and Claude through its hook.
- **Strength.** This is an instruction, not a lock.

### 4.7 Memory controls

**What the user sets.** "Use memory" pauses memory for every chat, and one switch per harness (Claude, pi) turns it off for just those chats. Notes are kept; chats neither use them nor save new ones.

**Storage.** The switches are stored in `data/.apps/memories/settings.json`, for example `{"is_paused": false, "disabled_harnesses": ["PI_CODING"]}` (`controls.py`). No file means memory is on everywhere, so nothing needs migrating.

**How each kind of chat is held to it:**

| Chat | Effect while off | When it applies |
|---|---|---|
| pi | Its two sections become one "memory is off" notice; the index is not given. | Next message. |
| New Claude chat at the workspace root | `autoMemoryEnabled: false` in `.claude/settings.local.json`, so Claude Code neither loads nor writes memory. The app merges this one key and removes it when Claude's memory is back on. | Chat start (verified). |
| Open Claude chat, or a worker in its own worktree | The hook prints the "memory is off" notice on every message, then says once that memory is back on and lists what was saved meanwhile. The per-chat mark keeps its time while memory is off. | Next message. |

**Failure handling:**
- **Unreadable settings count as off.** Reading them as on would ignore a user who turned memory off. The page names the problem and offers to replace the file.
- **A Claude settings file that is not a JSON object** refuses the save before anything is written.

### 4.8 The Agent Memory app (`system/apps/memories`)

The app is a Flask backend on port 8050 with a Mithril frontend, built with the shared `@imbue/workspace-ui` components.

**Process:**
- `stop_when_no_windows = true`, so the shell stops it a minute after its last window closes.
- OOM band 85, the most expendable built-in, since it holds nothing between requests.
- It is wired into the image build (`system/Dockerfile`), update-self's frontend list (`.agents/skills/update-self/scripts/update_layout.py`) and `system/test_app_manifests.py`.

**Routes** (`pages.py`):
- `GET /api/notes`: the notes, each with its attribution and whether its index line is loaded; the index's size and missing files; the backups' retention; the switches; and messages for anything unreadable.
- `PUT /api/notes/<file>` and `DELETE /api/notes/<file>`: correct or delete a note. Both carry the version the page read.
- `PUT /api/controls`: the switches.
- `GET /api/health`, the page, its assets and the shell's contract module.

**Attribution** (`attribution.py`):
- **Writers and readers** come from the chats' own transcripts:
  - every `Write`/`Edit`/`Read` tool call on a note's file in Claude transcripts, under each account's `projects/*/*.jsonl`;
  - the same calls in pi sessions, under `agents/<id>/plugin/pi_coding/sessions/`.
- **Session to agent to chat.** A Claude session maps to an agent through `claude_session_id_history`, and an agent to a chat through the chat app's `GET /api/chats`.
- **Writer kinds.** A writer is a live chat (shown by its title), an agent no live chat holds (a deleted chat or a background task), or unknown (for example when the chat app can't be reached).
- **Fallback.** A note no transcript explains falls back to its `source` field ("Saved by a pi chat").
- **Cost.** Transcripts are read per request. Each is searched as bytes in 4MB chunks, and only lines that name the notes folder are decoded and parsed (`lines_containing`). The request to the chat app for chat names logs a warning when it takes over a second. Section 7 has the measured cost.

**Writes** (`notes.py`):
- **Versions.** A note's version is `mtime_ns-size`, taken before its text is read, so a chat writing between the two can only make a save fail.
- **Conflicts.** A stale version gets a 409. The editor then shows the chat's version beside the draft and replaces it only on "Replace with my version".
- **Atomic writes.** Writes go through a uniquely named temporary file and a rename, keeping the file's permissions.
- **Order of a write.** The note is changed first, then the change is recorded (so open chats are told even if a later step fails), then its index line is updated under the index lock. An index failure is logged, not returned: the note change already happened, and the next sync, before any chat's next message, repairs the line.
- **Any visible Markdown file is a note**, whatever a chat named it (spaces, accents, a leading underscore); names with a path, hidden files and `MEMORY.md`/`README.md` are refused. Block-scalar descriptions read as none, and a repeated key reads as its first value, the one an edit rewrites.
- **Delete is permanent.** It removes the note's file and its `MEMORY.md` line. The confirmation says what still holds the note: backups for this workspace's actual retention, read through `host_backup.config`; open chats' conversations (they are told); and the transcripts of chats that read it.

**Write guard** (`request_guard.py`): a write must be JSON, which a browser won't send cross-origin without a CORS preflight that is never approved, and must carry `Sec-Fetch-Site: same-origin`. A request with no `Sec-Fetch-Site` (curl, an agent's script) comes from inside the workspace and is allowed. Comparing Origin with Host fails behind the forwarding proxy.

**Page** (`frontend/src/views/MemoriesPage.ts`; the computed wording, such as chips and counts, is in `format.ts`):
- **Top.** One sentence and three facts: which chats use the notes, not shared with other workspaces, and how long backups keep deleted notes.
- **"How memory works"** opens what gets saved, what chats use (including the load limit and how much of the user's index is loaded), where notes go (stored here and in backups, read by Claude and pi chats, sent to the chat's AI provider, not synced to GitHub) and the technical details.
- **"Settings"** opens the switches.
- **The warning line** appears only when the index needs attention: notes not in it, notes past the load limit, or lines for missing files.
- **Notes** are grouped by type. Each is a short card (summary, writer, date, Edit, Delete); the full text and the raw file open on "Show more".

## 5. Decisions and alternatives

| Decision | Alternatives considered | Why |
|---|---|---|
| One store in Claude Code's format | A new database or format; Claude's API memory tool (`memory_20250818`) as the interface | Claude already writes this format natively; anything else means syncing two stores or turning off Claude's built-in memory. Markdown files are readable by every harness's file tools. |
| Give pi the index in its system prompt, refreshed every turn | A pi memory tool or MCP server | Matches what Claude Code does (index up front, files on demand) with no new tool surface. Two stable sections keep pi's transcript from re-recording memory on every message. |
| Permanent delete, honest about backups | Trash with undo | Feedback asked for real deletion. Removing a note from restic snapshots is out of scope, so the UI states the retention instead of over-promising. |
| Keep the index line in code (4.5) | Firmer instructions; a warning in the app | Instructions already failed once (the pi chat). A warning leaves chats starting from the wrong fact until the user notices. |
| Per-chat mark for Claude's hook | Detect the chat's last reply in its transcript | Measuring from the last reply missed notes saved between the previous hook run and that reply, and depended on guessing which transcript lines are replies. |
| Unreadable settings mean off | Treat as on (fail open like the rest) | Failing open here would silently ignore a user who turned memory off. |
| `Sec-Fetch-Site` write guard | Compare Origin with Host | Host is rewritten by the forwarding proxy, which broke every write from the real page (found in testing). |

## 6. Privacy and security

**Where notes go:**
- Notes stay in the workspace's home directory and its backups. `data/` is gitignored, so they are never pushed to GitHub.
- A chat sends the index with every request to its model provider, plus a note's text when it opens one. The page says so.

**What is recorded about the user's actions:** the settings hold no note content. The change record holds a deleted or edited note's file name for 30 days, which often summarises the fact.

**What a delete leaves behind:** the transcripts of chats that read the note keep its text, and those of pi chats and of Claude chats told about it by the hook keep its summary line. Backups keep everything until they expire. The page says so in the delete dialog.

**Rules for chats** (instruction-level):
- never save secrets;
- don't save sensitive personal details unless asked (verified live: a pi chat and a Claude chat both declined to save a sensitive detail);
- treat notes as data rather than instructions, which mitigates a poisoned note now reaching every harness.

**Write guard:** see 4.8. The app reads all transcripts but returns only tool-call metadata (writer, time, reader count), never transcript text.

## 7. Limitations

- **Instruction-level enforcement.** "Memory off" is enforced by Claude Code only for new Claude chats at the workspace root. Open Claude chats, workers in their own worktrees (which don't read the root's `settings.local.json`) and pi chats are *told*. pi no longer receives the index, but nothing blocks a file write. The deletion notice is likewise an instruction.
- **A chat still knows its own conversation.** Pausing memory, or deleting a note, can't remove what the user told that chat directly. The page says so.
- **An open Claude chat started while memory was off** picks memory back up only after a restart, because Claude Code reads its switch at chat start.
- **pi chats started before the extension was deployed** don't load it until they restart. The same applies to any later change to `memory.ts`.
- **Backups keep deleted notes** until their retention expires (24 months by default).
- **A chat editing `MEMORY.md` directly** doesn't take the index lock, so its line can be lost to a concurrent rewrite; the next sync restores it from the note. A shell command that writes notes without naming `data/memories` (for example `cd data/memories && ...` from another directory) is synced only at the next message.
- **Unverified:** after Claude Code compacts a long conversation, the hook's earlier notices (for example a deletion) may not survive the summary.
- **Transcript reading grows with history.** Measured on macOS (warm cache; not gVisor, where file-heavy work is several times slower):
  - **The page** reads every Claude and pi transcript on each load (when the window opens or regains focus). The live workspace has 11 transcripts, about 5MB, and a page request takes 10 to 25ms. A synthetic heavy history of 200 chats (350MB) takes 0.12s to scan, down from 0.21s before searching bytes instead of decoding every line (0.2s cold). About 0.075s of that is the byte search itself, which any approach that re-reads every transcript must pay, so a further cut needs the cache in section 10. The cost scales with total transcript bytes, not with the number of notes, and nothing runs while the app is closed.
  - **Claude's hook** re-reads its own chat's transcript before every message: 13ms for a 20MB conversation, which is long. That's small next to a model turn, so it was left as is; section 10 has the fix if very long chats make it matter.
- **Only Claude and pi** use the notes. The page says Codex, OpenCode and Antigravity don't yet.
- **Known debt:**
  - The shared `Badge` warning and success tones fall below WCAG AA contrast; the app overrides its amber chip locally (`style.css`), and the shared fix is a design-system call.
  - `serving.py` duplicates static-serving code other apps also carry.

## 8. Testing

**Automated** (all green on the top branch):

| Suite | Count | Covers |
|---|---|---|
| App, `cd system/apps/memories && uv run pytest` | 157 | Note parsing and rendering round-trips (quoting, missing frontmatter, unknown keys); version checks and refusals; atomic writes; delete and index maintenance; the index's load limit, including exactly 25KB and one byte over; attribution over fake Claude and pi transcript trees, and the byte-level transcript reader (lines split across chunks, a last line without a newline, bad UTF-8, empty files); backup retention; the write guard's header matrix; the switches and the Claude settings merge (other keys kept, a non-object file refused), with the same settings table the script uses; odd note names, block scalars, repeated keys, kept permissions, the index lock; a delete recorded even when the index can't be rewritten; routes end to end through Flask; the app's ratchets. |
| Memory script, `system/scripts/agent_memory_context_test.py` | 61 | Rendering, truncation and stamping (CRLF, inline metadata, block scalars); the change notice; the Claude hook (notes since start, own writes skipped, once per chat, separate marks per session, no state dir, no transcript, off and back on); index sync (the Virginia case, adds, duplicates, gone notes dropped, untouched when in sync, permissions, CRLF on disk, waiting on the app's lock); every delete announced however many, and a count of notes past the list; a delete notice dropped once the note is saved again; the switches, by the same table as the app, including unreadable files as off; the PostToolUse command run exactly as `settings.json` has it; runs under `python3 -I` with a fake HOME. |
| pi extension, `.pi/extensions/pi_extensions_test.py -k memory` | 13 | `memory.ts` executed under node: the two sections, stable between messages, the forced-prompt path, stamping after `write` and `edit` through `~/`, relative and symlinked paths (and not outside the folder), a shell command on the notes syncing the index, the off notice, failing open with a log, including when `python3` is missing. |
| Frontend, `npm test --workspace=apps/memories/frontend` | 45 | The delete confirmation and what it says stays behind, the conflict editor, one edit at a time, a draft kept when a chat deletes its note, Save only after a change, collapsed cards and "Show more", index warnings, the condensed top section, the settings switches, their accessible names and what they save, buttons disabled while saving, and the requests: conflict and error messages, a refresh after each write, out-of-order refreshes ignored. |
| update-self | +1 | Every bundle's sources count as a frontend change, so the app's bundle is rebuilt on update. |

The changelog gate (`system/scripts/check_changelog_entries.py`) and `uv run app-manifest select-tests --diff-base <base>` were run per branch. Locally on macOS, the full root suite's only failures were environmental (browser tests without a Playwright browser, `os.waitid`, no `tmux`, long socket paths), and none touch this work.

**Verified live in a workspace container:**
- a pi chat recalls a note a Claude chat saved;
- pi writes a Claude-format note, stamped `source: pi-coding`, which the app credits to that pi chat;
- an open Claude chat learns of it on its next message;
- a deleted note isn't saved again by a chat that still had it in context;
- both harnesses decline a sensitive detail;
- memory survives `tk_workflow.ts` in both load orders;
- pi records the protocol once and the index only when it changes;
- writes work through the forwarding proxy;
- index sync on a copy of the real notes fixes the Virginia line;
- `autoMemoryEnabled: false` stops a new Claude chat recalling a note;
- the switches, driven in a browser, write the expected files and keep the other keys in Claude's settings.

## 9. Rollout

- **Nothing to migrate.** Existing notes are read as they are.
- **The index changes once.** The first sync rewrites existing `MEMORY.md` lines whose text differs from their note's description, for example Claude's short hooks becoming the description. This is the invariant from 4.5, and it changes only the index, never a note.
- **Picking up the change.** Chats pick up hook and script changes on their next message. pi chats pick up `memory.ts` changes when they restart.
- **Turning memory off fully.** Pause it in the app. Removing the hooks, the extension and the app reverts to Claude-only memory with no data loss.

## 10. Next steps

In rough priority order:

1. **Harder enforcement while memory is off.**
   - For pi: block `write` and `edit` into the notes folder in a `tool_call` handler, like `policy_guards.ts`.
   - For Claude: a `PreToolUse` guard on `Write`/`Edit` into the folder.
   - Shell writes would remain instruction-only.
2. **More harnesses.**

   | Harness | Read path | Notes |
   |---|---|---|
   | Codex | A `SessionStart` hook running `agent_memory_context.py --harness codex` | Keep its native memory off (`features.memories = false`): its path is per agent and its format incompatible. |
   | Antigravity | A rule file (`trigger: always_on`) including `MEMORY.md` | Include syntax unverified on 1.1.22. Switch off its own Knowledge Items. |
   | OpenCode | `instructions` = [protocol, `MEMORY.md`] | Not user-selectable today, so lowest priority. |

3. **Private chats** (designed only).
   - **Creation.** A chat created with `--env MINDS_MEMORY_MODE=private` (plus `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` for Claude) gets the off notice from the same script.
   - **Account switches.** The flag must be carried over when a chat switches account (`agent_manager.py`).
   - **Marker.** The chat header shows a persistent "Private" marker.
   - **Size.** This touches chat creation, so it is a separate PR.
4. **"Saved after you said..."** Show the user message that led a chat to save each note, from the same transcript scan.
5. **Incremental transcript reading**, if workspaces with very large histories show slow page loads (measure under gVisor first):
   - **The page:** keep each transcript's results keyed by path, size and mtime, and on the next load read only what was appended. Transcripts are append-only JSONL, so read from the last complete line. A shrunk or replaced file is read in full. The app stops a minute after its window closes, so an in-memory cache helps repeated loads while the window is open; making the first load fast too means persisting it under `data/.apps/memories/`, with a format version.
   - **Claude's hook:** store the byte offset already read, the start time and the chat's own writes in the per-chat mark file it already keeps, and read only the new part.
6. **Deleting from backups.** An option to remove deleted notes from backups (restic `rewrite`), or to exclude `data/memories/` from backups. This is a product decision.
7. **Shared contrast fix and cleanup.** Fix the shared Badge contrast tones, fold `serving.py` into a shared helper, and run pi's memory behaviour through the nightly evals before wider rollout.

## 11. Code map and branches

| Path | What |
|---|---|
| `system/apps/memories/src/memories/` | Backend: `notes.py` (format, index, writes), `attribution.py`, `backups.py`, `changes.py`, `controls.py`, `request_guard.py`, `pages.py` (routes), `main.py`, `config.py` |
| `system/apps/memories/frontend/src/` | Page: `views/MemoriesPage.ts`, `views/format.ts` (computed wording), `models/notes.ts` (data and requests) |
| `system/scripts/agent_memory_context.py` | What every chat is told; stamping; index sync; the switches |
| `.pi/extensions/memory.ts` | pi wiring |
| `.claude/settings.json` | Claude's UserPromptSubmit and PostToolUse hooks |
| `.agents/shared/references/memory-protocol.md` | The protocol given to harnesses without built-in memory |
| `CLAUDE.md`, `AGENTS.md`, `.mngr/settings.toml`, `data/memories/README.md` | Memory rules and wording for the agents |

Branches are stacked, each based on the one before:
1. `natalie/memories-app`: the app.
2. `natalie/shared-memory-pi`: pi, the script, the hooks, notices and index sync.
3. `natalie/memories-transparency`: the Agent Memory name, the condensed page, load-limit transparency, contrast.
4. `natalie/memory-controls`: the switches and this document.
