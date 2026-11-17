# history_sweep

Frees the disk a deleted chat leaves behind, without touching anything a chat the
chat app still lists can open. The `history-sweep` routine
(`system/routines/history-sweep.toml`) runs it every day at 04:00; the Routines app
shows each run and its summary.

## What a deleted chat leaves

Deleting a chat destroys its agents. For each one:

- mngr keeps a preserved archive under `~/.mngr/preserved/<name>--<id>/`: the raw
  transcript, the common transcript `mngr transcript` and the find-transcripts skill
  read, and the agent's session-id history;
- claude's session files for the agent stay in the account folder
  (`~/.minds/accounts/<account>/projects/<dir>/<session>.jsonl`, the session's
  subagent folder beside it, and `file-history/<session>`). Nothing opens them once
  the agent is gone, and the archive's raw transcript holds the same lines.

## What the sweep removes

For each archive whose agent is neither present under `~/.mngr/agents/` nor named
by any chat record (`data/.apps/chat/chats/*/record.json`):

| When | What |
| --- | --- |
| At once, if the archive holds a non-empty raw transcript | the agent's session files in every account folder |
| 30 days after the archive was written (`preserved_at` in its manifest) | the archive, and any session files still left |

It never removes a session that a present or listed agent's history also names, never reads
an archive whose manifest it cannot parse, and removes nothing at all when a chat
record cannot be read (it cannot tell what is listed). Archived hand-off segments
of a listed chat are present agents, so they are never touched.

Chats that are still listed keep their history: the template also sets claude's
`cleanupPeriodDays` to 3650 (`.mngr/settings.toml`), since claude otherwise deletes
session files older than 30 days when it starts.

## Command

`uv run --no-sync history-sweep [--dry-run]` from the repo root. Each removal is one
line (`removed <reason> <agent id> <bytes> <path>`), then a one-line summary, which a
routine run also writes to its `result.json`. `--dry-run` prints what would go and
removes nothing. Paths come from `MNGR_HOST_DIR` (default `~/.mngr`) and
`MINDS_ACCOUNTS_ROOT` (default `~/.minds/accounts`).
