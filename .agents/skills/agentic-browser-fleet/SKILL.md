---
name: agentic-browser-fleet
description: Drive the workspace's shared Chromium browser yourself, one command at a time, from your shell. Reached from the connect-external-service skill, which decides when a browser is the way to do something on the web for the user (log in somewhere, fill a form, click through a flow, read a page that needs interaction); load that skill first rather than this one. YOU own the browser and YOU drive it -- in this same chat, with your own reasoning.
metadata:
  author: imbue
---

# Driving the browser

Two commands, and the split matters:

- **`agentic-browser-fleet`** *owns* the browser -- start it, list it, hand it to the human,
  give it back. Run from the repo root via `uv run --no-sync`.
- **`playwright-cli`** *drives* it -- look at the page, click, type, scroll.

```bash
uv run --no-sync agentic-browser-fleet new
  -> started browser browser-1
     drive it:  playwright-cli -s=browser-1 attach --cdp=http://127.0.0.1:8083/browser-1/<token>
     then:      playwright-cli -s=browser-1 <command>
```

Run that `attach` line once. After that it is plain `playwright-cli -s=browser-1 <command>`.

**`playwright-cli --help` is the command reference.** It ships with the pinned version, so it is
always correct -- this skill deliberately does not restate it. What this skill covers is
everything `--help` cannot know: who owns the browser, what the human can do to it underneath
you, and the handful of ways this goes wrong in practice.

**No API key needed.** Everything here is deterministic and keyless.

## First: the browser runs on the Screen, and `new` gives it to you

The browser is one Chromium (`browser-1`, shown as "Browser" in the workspace UI) running on the
**Screen**, the workspace's one streamed Linux desktop (the `use-screen` skill). It shares that
desktop with everything else the user runs there: a terminal, another program, a second Chromium
window the user opened. `new` prints its **name** and the attach line: it starts the browser the
first time (starting the Screen too when it is down), and after that it answers the same browser,
starting it again if it was stopped. The browser is addressed by name everywhere.

- `new` returns as soon as the browser is registered; **the Screen and Chromium may still be
  starting**. If the attach line says it is still starting, wait a few seconds and run `ls`.
- `new` also opens the Screen's window on the user's desktop, **minimized**: it waits in their
  taskbar, and they do not see it until they click it or you show it (see "Live view" below).
  That is right while you browse on your own; show it once they should watch. `handoff` shows
  it for you. **The browser does not stop when the user closes that window**: the Screen never
  stops for emptiness, and Chromium keeps running until the user quits it from inside, something
  stops it (`POST /browsers/browser-1/stop` on the Screen service) or the Screen itself is quit.
  Never close the user's window yourself.
- `new my-browser` asks for a second, named browser; the fleet is capped at one Chromium (a second
  is more memory than a small workspace can afford) and answers `1/1 browsers open -- close one
  first`. Do not do this.
- **The browser cannot be renamed.**

## The lease is the Screen's

Owning the browser means holding the **Screen's control lease**: one controller at a time, human or
agent, for the whole desktop. Every verb below acts on that lease, so `acquire` on the browser and
`screen control acquire` are the same thing, and a human taking control of the Screen (from its
window's strip) takes the browser with it.

## The loop

1. `playwright-cli -s=<name> snapshot` (or `find "text"`) -- see what is on the page.
2. Decide what you want.
3. Act: `click <ref>`, `fill <ref> "text"`, `press Enter`, `hover <ref>`, `mousewheel 0 800`...
4. Look again. Repeat.

Refs (`e12`) come from the latest snapshot. **Re-snapshot after anything that changes the page**
-- refs survive DOM mutation, so a stale ref can silently resolve to a *different* element
rather than erroring.

## Fleet commands

```bash
uv run --no-sync agentic-browser-fleet ls                      # the browser: name, who holds it, tabs
uv run --no-sync agentic-browser-fleet ls --include-tabs       # every tab
uv run --no-sync agentic-browser-fleet new                     # the browser (started if stopped); prints its name + attach line
uv run --no-sync agentic-browser-fleet close <name>            # rarely: end it AND DELETE its profile (logins)
uv run --no-sync agentic-browser-fleet acquire <name>          # reserve it (reprints the attach line)
uv run --no-sync agentic-browser-fleet acquire <name> --reclaim   # take back from a human -- only if they said so
uv run --no-sync agentic-browser-fleet release <name>          # hand it back (alias: unlock)
uv run --no-sync agentic-browser-fleet handoff <name> "reason" # give it to the human (alias: request-human)
```

`ls` shows who controls the Screen the browser runs on (`you`, `agent <name>`, `human (took
control)`, `human (driving the Screen)`, or `free`), the browser's lifecycle when it is not up
(`starting`, `stopped`, `crashed`), its tab count, and its active URL.

`close` ends the whole browser and **deletes its profile (cookies, logins) with it**. For a single
tab use `playwright-cli tab-close`.

## Ownership rules

The Screen has exactly one controller. **The human always wins, instantly** -- the moment they
take control, your very next command is refused mid-session.

- **You auto-acquire and hold a sticky lease.** No manual `acquire` needed for normal driving;
  your first command takes a free Screen.
- **The lease goes idle after ~60s with no commands.** Holding the connection open is not enough
  -- only actual commands count. If you step away and come back, just carry on; if it expired,
  re-acquire.
- **Release when the browser leaves your active work** (`release <name>`), so control returns to
  the human immediately rather than after the idle timeout: task finished, or the user told you to
  stop.
- **The human takes over.** Your next command fails. **Stop, tell the user the human took the
  wheel, and end your turn.** Do not retry or `--reclaim` on your own. You are queued to resume
  first and will be messaged; on resume, take a fresh `snapshot` -- the page changed, and the
  Screen may have been resized while they held it, so every ref is stale.
- **A human merely using the Screen holds it too.** Someone typing into a terminal on the Screen
  is its controller ("human (driving the Screen)" in `ls`); you queue behind them like behind a
  take-control, and are messaged when the Screen frees.
- **Agents never preempt each other.** A Screen another agent holds is theirs; `acquire` queues you
  for it and you are messaged when it frees.
- **Never `--reclaim` a Screen a human is holding on your own initiative.** End your turn and
  ask. A bare "keep going" from the user counts as confirmation.

## When something fails, run `ls` -- do not read the error

**`playwright-cli` exits 1 for every failure and cannot tell you why.** A revoked lease, a
crashed browser and a stale ref all look identical; a refused command may report
`Execution context was destroyed` or simply time out after 30s. None of that is diagnostic.

```bash
uv run --no-sync agentic-browser-fleet ls
```

That is the only thing that distinguishes "the human took control" from "the browser crashed"
from "your ref went stale". The fleet's own commands *do* return meaningful exit codes -- which
is why `acquire` is worth running before a long stretch of driving.

## Crashes, stops, and the Screen going away

- **The browser can crash** (Chromium killed, e.g. out of memory), and from your side it looks like
  your connection simply dying: a generic disconnect, not a crash. Run `ls`, which says
  `crashed`. Then run `new`, which starts the same browser again on its profile and tabs and
  prints a fresh attach line.
- **Do not re-attach under the old `-s` name.** Your `playwright-cli` session for it is dead too
  -- a session whose socket drops never rebinds, and every command then **hangs with empty
  output**. Attach under a new slug (`playwright-cli -s=browser-1b attach --cdp=...`) and drive
  with that slug from then on.
- **The Screen can end** (the user quit it, or memory pressure shed its stack): Chromium goes with
  it and `ls` says `stopped`. `new` starts the Screen and the browser again, on the saved tabs.
- **Same if the browser is closed or stopped while you are attached**: the connection dies, `ls`
  says so, start over with `new` and a fresh slug.
- **The browser persists across a workspace restart** -- tabs, order, and cookies/logins all come
  back, so a site you logged into earlier is probably still logged in. Right after a restart the
  Screen may still be starting; `ls` works, driving may need a few seconds.

## Long lists: "not found" is not the same as "not there"

The most expensive mistake on this tool. On a **virtualized** list only the visible rows exist in
the DOM, so `find` and `snapshot` genuinely cannot see the rest. An agent that searches, gets
`No matches found`, and reports "there are only 25 items" is wrong, and confidently so.

**Never conclude a list is complete from a snapshot alone.** If a list scrolls, the only way to
know what is in it is to scroll to the end.

Check what kind of list you have before trusting any count:

```bash
playwright-cli -s=<name> eval "() => document.querySelectorAll('[role=row],li,tr,a').length"
playwright-cli -s=<name> mousewheel 0 800
playwright-cli -s=<name> eval "() => document.querySelectorAll('[role=row],li,tr,a').length"
```

- **Count flat, content changed** -> virtualized. Rows are recycled; `find` only ever sees the
  current window. Scroll to the end.
- **Count grew** -> infinite scroll. New rows are appended, usually after a network fetch, so
  give it a moment before searching again.
- **Count already large, barely moved** -> an ordinary long page. One scroll to the bottom does it.

To work a virtualized list: `hover` a row inside it (so the wheel lands on the right container),
then repeat `mousewheel 0 800` + `find`. Roughly 20 rows per 800px. **You are at the bottom when
the visible rows stop advancing between wheels -- not when `find` fails.**

## Scrolling the thing you meant

`mousewheel` scrolls at the **last mouse position, which defaults to (0,0)** -- the top-left
corner. On a two-pane layout that is the sidebar, so you can scroll and see nothing move.
`hover <ref>` inside the pane you mean first. If a scroll appears to do nothing, this is the
first thing to check.

## Snapshots are cheap, screenshots are not

Default to `snapshot` / `find`: text, cheap, and they give refs you can act on. `snapshot` prints
the whole tree inline (5-15k tokens on a real page) -- prefer `find "text"` to locate something,
`eval` to read one value, and `snapshot --filename=/tmp/page.yml` when you truly need the tree.
The snapshot that comes back automatically after each command is a file link and costs nothing.

Take a **screenshot** only when the accessibility tree cannot answer the question:

- the content is a **canvas, chart, map, video or PDF viewer** -- there is no tree to read
- the question is **visual**: is this button disabled, is the row highlighted, did the layout break
- the snapshot is **empty or nonsense** but the page clearly rendered
- you are **stuck**: two attempts failed and the text does not tell you why

`playwright-cli screenshot` renders the page itself and needs no window in front. A screenshot of
the **Screen** (`uv run --no-sync screen shot`) shows whatever is frontmost on the desktop, which
is not the browser unless you raised it: `uv run --no-sync screen windows` lists the X windows
and `uv run --no-sync screen activate <id>` brings Chromium's to the front first.

Do not screenshot to check a page loaded -- the per-command snapshot already gives URL and title.

## Hitting a wall a human must clear (CAPTCHA / 2FA / login)

CAPTCHA, "verify you're human", an SMS/2FA code you don't have, or a login needing the user's own
credentials: **do not try to solve it yourself** -- you will fail and may get the account flagged.

```bash
uv run --no-sync agentic-browser-fleet handoff browser-1 "solve the CAPTCHA on the sign-in page"
```

`handoff` puts you at the **front** of the resume queue, hands the Screen to the human pinned (no
other agent can take it until they give it back; their first input claims it, and their "Give
control back" or leaving hands it to you), and brings the Screen's window up in front of them with
Chromium raised on it. In the **same turn**: tell the user exactly what to do and on which page,
then **end your turn**. You are woken first when they hand it back -- re-`snapshot` to confirm the
challenge cleared, then carry on.

## Live view vs. your output

The Screen streams to a window on the desktop of whoever is watching your chat, and Chromium's
window on it follows whatever tab you are acting on. `new` and `acquire` open that window
automatically, but minimized, and only when the shell can tell which screen asked (the client that
last messaged your chat while it is connected, else the one connected client if it is the same
user's, else that messaging client though it has gone); `handoff` opens it in front. To put it in
front of the user at any other time -- they asked to see the browser, or should watch what you do
-- show it yourself:

```bash
uv run --no-sync screen open --show
uv run --no-sync screen activate <chromium window id>   # from `screen windows`, if something covers it
```

That restores and raises the window, or opens one if there is none. It changes what the user is
looking at the moment it returns, so run it when you want them to look, and never tell them to
open a window you can show them (the `manage-desktop` skill has the rest of `workspace-layout`).

The window is **viewer only** -- your real output is here in the CLI. Read and relay it; never tell
the user to "check the window" for results.

## Tabs and sub-agents

- **Tabs:** `playwright-cli tab-list` / `tab-new` / `tab-select` / `tab-close`, within the
  browser. The fleet's `ls --include-tabs` shows the same tabs in the same order. A page's
  pop-up (a "Sign in with ..." button) opens as a tab of the same window.
- **Drive the browser yourself, in this chat.** A `launch-task` sub-agent runs in a separate
  worktree of this same workspace, so it could reach the browser, but the browser and the Screen
  belong to this chat: the user watches and takes over here. If a sub-agent needs something
  from the web, have it tell you what it needs and you do the browsing.

## Exit codes -- branch on these

These are the **fleet's** exit codes. `playwright-cli` has its own and they are not this granular
(see "run `ls`" above).

| Code | Meaning | What to do |
|---|---|---|
| `0` | ok | Carry on. |
| `1` | error | Read the message. |
| `2` | preempted (human took control, or you ran `handoff`) | **Stop and end your turn.** You'll be messaged to resume; re-`snapshot` first. |
| `3` | busy (another agent holds the Screen, or the Screen / Chromium is still starting) | Wait a few seconds and retry; for another agent, you are queued and will be messaged. |
| `4` | timed out waiting for another agent | Try later. |
| `64` | usage (no chat id: `MINDS_CHAT_ID` and `MNGR_AGENT_ID` both unset / bad arguments / invalid name) | Run from inside an agent shell; fix the command. |
| `69` | no service (can't reach the Screen service) | The service isn't running -- report it; don't blindly retry. |

## Quick recipes

```bash
# Start the browser and start driving.
uv run --no-sync agentic-browser-fleet new                       # prints the attach line -- run it once
playwright-cli -s=browser-1 goto https://example.com
playwright-cli -s=browser-1 find "More information"    # cheaper than a full snapshot
playwright-cli -s=browser-1 click e14

# Search and submit with the keyboard.
playwright-cli -s=browser-1 goto https://news.ycombinator.com
playwright-cli -s=browser-1 fill e3 "browser automation" --submit

# Read every row of a long list (see "Long lists" above for why this shape).
playwright-cli -s=browser-1 hover e12                  # put the wheel over the list
playwright-cli -s=browser-1 mousewheel 0 800
playwright-cli -s=browser-1 find "the thing you want"  # repeat until rows stop advancing

# Hit a CAPTCHA -- hand it over, then STOP.
uv run --no-sync agentic-browser-fleet handoff browser-1 "solve the CAPTCHA on the sign-in page"
```

## Don'ts

- **Don't run `playwright-cli close`, `attach` (twice), `detach`, `close-all`, `kill-all` or
  `delete-data`.** The fleet owns lifecycle: the browser runs until the user quits it or the Screen
  ends. `agentic-browser-fleet close <name>` deletes the profile (every login) with it, so use it
  only when the user asks for a fresh browser.
- **Don't re-attach a slug whose session died** -- see "Crashes" above. It hangs rather than
  erroring, which is the one failure you cannot recover from; use a new slug.
- **Don't interpret a `playwright-cli` failure.** Run `ls`.
- **Don't conclude a scrollable list is complete** because `find` came up empty.
- **Don't `--reclaim` a Screen a human is holding** without them explicitly saying so.
- **Don't try to solve a CAPTCHA or 2FA challenge.** Hand it off.
- **Don't rely on `screen shot` to show the page** without raising Chromium's window first; the
  Screen shows whatever is frontmost.
