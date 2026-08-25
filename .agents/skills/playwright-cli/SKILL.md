---
name: playwright-cli
description: Drive a browser from the shell -- snapshot the page, click, type, scroll. Use with the agentic-browser-fleet skill, which OWNS the browsers this drives.
metadata:
  author: imbue
---

# Driving a fleet browser

`agentic-browser-fleet new` prints the one `attach` line to run. After that you drive with
plain `playwright-cli -s=<browser> <command>`.

**Run `playwright-cli --help` for the command list** -- it is the authority and it ships with
the pinned version, so it can never drift the way a copy here would. This file only covers the
five things `--help` does not know about this workspace.

**1. The fleet owns lifecycle, so never run `close`, `attach`, `detach`, `close-all`,
`kill-all`, or `delete-data`.** `new` already attached you. Ending a browser is
`agentic-browser-fleet close <name>`, which also retires the name and deletes the profile.

**2. A torn-down session is unrecoverable.** If a slug's daemon is detached or killed it is
poisoned: re-attaching under the same name never rebinds and every command then hangs with
empty output. There is no recovery except a new browser.

**3. On ANY failure, do not read the message -- run `uv run agentic-browser-fleet ls`.**
`playwright-cli` exits 1 for everything, so a revoked lease and a stale ref are
indistinguishable, and a refused command may report `Execution context was destroyed` or just
time out. `ls` is the only thing that tells you whether the human took control, the browser
crashed, or your ref went stale.

**4. `snapshot` prints the whole tree inline** -- 5-15k tokens on a real page. Prefer
`find "text"` to locate something, `eval` to read one value, and `snapshot --filename=...`
when you truly need the tree. The snapshot that comes back automatically after each command
is a file link and costs nothing.

**5. `mousewheel` scrolls at the LAST MOUSE POSITION, which defaults to (0,0)** -- the
top-left corner. On a two-pane layout that is the sidebar, and it will silently scroll the
wrong thing. `hover <ref>` inside the pane you mean first. For a virtualized list, repeated
`mousewheel` works well (~20 rows per 800px); confirm progress with `find`, not a fresh
`snapshot`.

Two smaller notes: **re-snapshot after any action that changes the page** -- refs survive DOM
mutation, so a stale ref can resolve to a *different* element rather than erroring. And
`goto file://...` is blocked by the CLI; serve a local file over http if you need one.
