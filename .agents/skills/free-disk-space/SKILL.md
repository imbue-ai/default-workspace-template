---
name: free-disk-space
description: Walk the user through freeing disk space with the Disk Space app. Run as the first message of the "Free up disk space" chat the app opens when the disk passes its alert line (or when the user presses "Ask the cleanup helper"); also use whenever the user says the disk is full, asks what is using the disk, or wants space freed.
---

# Free disk space with the user

The Disk Space app (`system/apps/disk_space/`, window "Disk Space") measures the disk and sorts every
large folder into **safe to delete** (caches that download again, stale scratch), **ask first** (the
user's own folders, project copies, app data, old chats, recent scratch) and **keep** (what the
workspace runs on). This chat exists to get the disk back under the line without losing anything
the user cares about.

The message that started this chat says why it was started (the disk passed the line, or the user
asked) and may name one item the user asked about. Start with that item when there is one.

## 1. Tell the user and show the app (only when the disk passed the line)

When the first message says the disk passed the alert line, send one notification, then open the
app beside this chat:

```bash
python3 .agents/skills/notify-user/python/notify_user_skill/notify_user.py "The disk is <N>% full. Open the Free up disk space chat to clear some room."
uv run --no-sync workspace-layout open disk-space --beside
```

When the user started this chat from the page, the app is already open: skip both.

## 2. Read the state

```bash
system/apps/disk_space/.venv/bin/disk-space status --json
```

`measured_minutes_ago` over about 30 means the breakdown is stale: run
`system/apps/disk_space/.venv/bin/disk-space scan` (about a minute) and read the status again.

## 3. Offer the safe items first, in one message

List the safe-to-delete items with sizes and their total, say in one line they download again or
rebuild when needed, and ask once: "Delete these?" On yes:

```bash
system/apps/disk_space/.venv/bin/disk-space delete <path> <path> ... --by helper
```

Report what it freed (it prints "Freed X GB") and the new percentage.

## 4. Then the ask-first items, biggest first, a few at a time

For each, say what it is in plain words and what deleting it costs, and check before claiming
anything is safe:

- **Project copy** (`.external_worktrees/...`) or **background task copy** (`worktrees/...`), or a
  scratch folder holding a project: run `git -C <path> status --short` and
  `git -C <path> log --branches --not --remotes --oneline` first. Uncommitted or unpushed work
  means the user would lose it: say so plainly.
- **App data** (`data/.apps/<app>`): say whether the app can fetch it again (read the app's README if
  unsure). Deleting it resets that app.
- **Your folder** (`data/<name>`) and **uploads**: only the user knows. Offer to show it in Files.
- **Scratch** (`/var/tmp/...`) marked recent: something used it this week; ask what it was.
- **Chat files** (`~/.mngr/agents/<id>`): these are chats. Never delete the folder. A finished chat
  the user agrees to remove is ended with `mngr destroy <agent-id>`; never this chat, never one that
  is running or that the user has open. Its transcript stays readable afterwards.

Delete only what the user said yes to, naming the items back to them first:

```bash
system/apps/disk_space/.venv/bin/disk-space delete <path> ... --user-approved --by helper
```

`--user-approved` is a promise that the user said yes to these exact items in this chat. Never pass
it on your own judgement.

## 5. Finish

Stop once the disk is comfortably under the line (aim for 10 points below it) or the user is done.
Say the new percentage and what was freed. The app's history records every deletion; the page
refreshes on its own.

## Never

- Delete anything under "keep", anything the latest measurement did not list, or anything outside
  `disk-space delete` (no hand-run `rm -rf` on the user's folders).
- Touch the backups themselves (`/var/cache/user/restic` is only the backup's cache).
- Raise the alert line to make the warning go away unless the user asks; if they do, it is the
  "Watching the disk" setting on the page or `POST /api/settings`.
