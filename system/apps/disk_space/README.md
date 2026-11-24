# Disk Space

Shows what fills the workspace's disk as a treemap, sorts every large folder into **safe to
delete**, **ask first** and **keep**, and starts a cleanup helper chat when the disk passes the
alert line (90% by default).

## How it fits together

- **The check** (`disk-space check`, run every 10 minutes by the `disk-space-check` routine): instant
  while the disk is under the line. Over it, it measures the disk (unless the last measurement is
  under 30 minutes old) and starts the cleanup helper. It alerts once per crossing, nudges again
  after `renotify_hours` (6) while the disk stays over, and re-arms when the disk drops back under.
- **The cleanup helper**: a "Free up disk space" chat made through the chat app with
  `auto_open=true`, whose first message runs the `free-disk-space` skill. That skill notifies the
  user, opens this app beside the chat, and walks through what to delete. A second request goes to
  the same chat while it exists.
- **The measurement** (`disk-space scan`, or "Measure again"): one `du` per group (workspace, chats,
  scratch, caches, installed tools, system), about a minute under gVisor, run as its own process
  under a lock. "Everything else" is the disk's used space less everything measured.
- **The verdicts**: `RULES` in `scan.py`; the first rule matching a folder decides it and everything
  in it. Scratch folders in `/var/tmp` are safe only when untouched for 7 days, hold no project
  history, and no process works in them. Anything no rule names is kept.
- **Deleting** (`cleanup.py`): only items of the latest measurement; safe ones from the page,
  ask-first ones only with `--user-approved` (the helper, after the user said yes); keep never.
  Chats are never deleted as folders.

## For the Caretaker or any other agent

One call answers "how full is the disk, and what can go?":

```
GET http://127.0.0.1:8099/api/status          (the app running)
system/apps/disk_space/.venv/bin/disk-space status --json   (always works)
```

It returns the percentage, the alert line, whether the disk is over it, the age of the breakdown,
and the largest safe-to-delete and ask-first items. `POST /api/delete {"paths": [...]}` frees safe
items; `POST /api/helper` starts the cleanup helper. The full API is at `/api/schema`.

`POST /api/delete`, `POST /api/helper` and `POST /api/settings` are **owner only**: once the workspace is shared,
a visitor's request for them is refused with 403 on the server, whatever the page shows. Reading what the app
measures (`/api/state`, `/api/status`, the raw records, the page itself) is open to anyone holding a grant. Every
route is behind the workspace's request guard, so a page of another origin can never call it.

## Data

`data/.apps/disk-space/`: `settings.json`, `watch.json`, `history.jsonl`, and the newest 5
`snapshots/<id>/` (the tree as `snapshot.json` beside the raw `du-<group>.txt` it came from). The
page links each history row and the measurement to its raw record (`/raw/...`).

Both stores are bounded, so the app cannot fill the disk it reports on: the history keeps its newest 500 entries
(older lines are dropped as new ones are written), and only the newest 5 measurements are kept. The measurement
folder carries a `.nobackup` marker, because a measurement can always be taken again: the workspace's backup keeps
only the marker, and a restored workspace simply measures the disk again.

## Small screens, and testing

The page fits a phone as well as a desktop: nothing scrolls sideways at 393px, a phone held sideways included, and
with a finger every control is at least 44px tall and the alert-line field is 16px, so iOS does not zoom the page.

```bash
cd system/apps/disk_space && uv run --no-sync pytest
```

That runs the app's tests from its own venv: the logic against planted measurements and planted folders in a scratch
data directory (never the app's real data), the routes through the test client, the ratchets, and the page in a
browser at desktop, phone and phone-landscape sizes. The browser tests launch the workspace's Chromium (Fortress) by
its own path.
