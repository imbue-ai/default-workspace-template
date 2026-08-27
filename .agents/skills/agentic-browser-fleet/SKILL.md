---
name: agentic-browser-fleet
description: Drive a fleet of shared Chromium browsers yourself, one command at a time, from your shell. Use when the user wants you to do something on the web (log in somewhere, fill a form, click through a flow, read a page that needs interaction) rather than just fetch a URL. YOU own the browser and YOU drive it -- in this same chat, with your own reasoning.
metadata:
  author: imbue
---

# Driving the browser fleet

Two commands, and the split matters:

- **`agentic-browser-fleet`** *owns* browsers -- start one, list them, hand one to the human,
  give it back. Run from the repo root via `uv run`.
- **`playwright-cli`** *drives* them -- look at the page, click, type, scroll.

```bash
uv run agentic-browser-fleet new
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

## First: there are no browsers until you make one

The fleet starts **empty**. `new` prints a **name** (numbered `browser-<N>`, shown as "Browser N"
in the workspace UI) and the attach line. Browsers are addressed by name everywhere.

- `new` mints the first free `browser-<N>`; `new my-browser` chooses the name (lowercase letters
  and digits joined by single dashes). A **duplicate** name is rejected -- note a *crashed*
  browser still holds its name until you `close` it.
- `new` returns as soon as the browser is registered; **Chromium is still launching**. If the
  attach line says it is still starting, wait a few seconds and run `ls`.
- The fleet is **capped (2 by default)**. `new` past the cap returns
  `2/2 browsers open -- close one first`.
- **Browsers cannot be renamed.** If the user asks, say so: the only option is `close` + `new`,
  which is a different browser with a fresh profile.

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
uv run agentic-browser-fleet ls                      # the whole fleet: names, owners, tabs
uv run agentic-browser-fleet ls --include-tabs       # every tab of every browser
uv run agentic-browser-fleet new [name]              # start one; prints its name + attach line
uv run agentic-browser-fleet close <name>            # close it, retire the name, DELETE its profile
uv run agentic-browser-fleet acquire <name>          # reserve it (reprints the attach line)
uv run agentic-browser-fleet acquire <name> --reclaim   # take back from a human -- only if they said so
uv run agentic-browser-fleet release <name>          # hand it back (alias: unlock)
uv run agentic-browser-fleet handoff <name> "reason" # give it to the human (alias: request-human)
```

`ls` shows who controls each browser (`you`, `agent <name>`, `human (took control)`, or `free`),
its tab count, and its active URL -- so you can pick one.

`close` ends a whole browser and **deletes its profile (cookies, logins) with it**. For a single
tab use `playwright-cli tab-close`.

## Ownership rules

Every browser has exactly one controller. **The human always wins, instantly** -- the moment they
take control, your very next command is refused mid-session.

- **You auto-acquire and hold a sticky lease.** No manual `acquire` needed for normal driving;
  your first command takes it.
- **The lease goes idle after ~60s with no commands.** Holding the connection open is not enough
  -- only actual commands count. If you step away and come back, just carry on; if it expired,
  re-acquire.
- **Release when a browser leaves your active work** (`release <name>`), so control returns to
  the human immediately rather than after the idle timeout: task finished, the user told you to
  stop, or you moved to a different browser.
- **The human takes over.** Your next command fails. **Stop, tell the user the human took the
  wheel, and end your turn.** Do not retry or `--reclaim` on your own. You are queued to resume
  first and will be messaged; on resume, take a fresh `snapshot` -- the page changed, and the
  view may have been resized while they held it, so every ref is stale.
- **Agents never preempt each other.** A browser another agent holds is theirs. Use a different
  one (or `new`); `acquire` only if you specifically need that browser and will queue for it.
- **Never `--reclaim` a browser a human is holding on your own initiative.** End your turn and
  ask. A bare "keep going" from the user counts as confirmation.

## When something fails, run `ls` -- do not read the error

**`playwright-cli` exits 1 for every failure and cannot tell you why.** A revoked lease, a
crashed browser and a stale ref all look identical; a refused command may report
`Execution context was destroyed` or simply time out after 30s. None of that is diagnostic.

```bash
uv run agentic-browser-fleet ls
```

That is the only thing that distinguishes "the human took control" from "the browser crashed"
from "your ref went stale". The fleet's own commands *do* return meaningful exit codes -- which
is why `acquire` is worth running before a long stretch of driving.

## Crashes and closed browsers

- **A browser can crash** (Chromium killed, e.g. out of memory), and from your side it looks like
  your connection simply dying: a generic disconnect, not a crash. Run `ls`, which says
  `crashed`. Then run `new` and use the attach line it prints.
- **Do not re-attach the old name.** Its Chromium is gone for good, the name stays reserved until
  someone `close`s it, and your `playwright-cli` session for that name is dead too -- a session
  whose socket drops never rebinds, and every command then **hangs with empty output**. `new`
  gives you a different name, so its session is clean.
- **Same if a browser is closed while you are attached**: the connection dies, `ls` says it is
  gone, start over with `new`.
- **Browsers persist across a workspace restart** -- tabs, order, and cookies/logins all come
  back, so a site you logged into earlier is probably still logged in. Right after a restart the
  fleet may still be restoring; `ls` works, driving may need a few seconds.

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

Do not screenshot to check a page loaded -- the per-command snapshot already gives URL and title.

## Hitting a wall a human must clear (CAPTCHA / 2FA / login)

CAPTCHA, "verify you're human", an SMS/2FA code you don't have, or a login needing the user's own
credentials: **do not try to solve it yourself** -- you will fail and may get the account flagged.

```bash
uv run agentic-browser-fleet handoff browser-1 "solve the CAPTCHA on the sign-in page"
```

`handoff` puts you at the **front** of the resume queue, hands control to the human (pinned, so
it will not pass to another agent), and surfaces the pane. In the **same turn**: tell the user
exactly what to do and on which page, then **end your turn**. You are woken first when they hand
it back -- re-`snapshot` to confirm the challenge cleared, then carry on.

## Live view vs. your output

The browser streams to a UI pane next to your chat, and it follows whatever tab you are acting
on. `new` and your first command surface it automatically -- but only when the user is currently
watching your chat. Do not manage panes yourself; if the user asks for a browser that is not
showing, tell them to open it from the workspace **+ -> browser** menu.

The pane is **viewer only** -- your real output is here in the CLI. Read and relay it; never tell
the user to "check the tab" for results.

## Multiple browsers, tabs, sub-agents

- **Multiple browsers:** `new` each one; they are independent and do not queue against each
  other. Drive several at once just by varying the name.
- **Tabs:** `playwright-cli tab-list` / `tab-new` / `tab-select` / `tab-close`, within one
  browser. The fleet's `ls --include-tabs` shows the same tabs in the same order.
- **Drive the browser yourself, in this chat.** A `launch-task` sub-agent runs in a separate,
  isolated container with no access to this workspace's fleet. If a sub-agent needs something
  from the web, have it tell you what it needs and you do the browsing.

## Exit codes -- branch on these

These are the **fleet's** exit codes. `playwright-cli` has its own and they are not this granular
(see "run `ls`" above).

| Code | Meaning | What to do |
|---|---|---|
| `0` | ok | Carry on. |
| `1` | error | Read the message. |
| `2` | preempted (human took control, or you ran `handoff`) | **Stop and end your turn.** You'll be messaged to resume; re-`snapshot` first. |
| `3` | busy (another agent holds it, or fleet full / still restoring / still launching) | Use a different browser (or `new`); for "restoring"/"starting up", wait a few seconds and retry. |
| `4` | timed out waiting for another agent | Try later, or pick a different browser. |
| `64` | usage (`MNGR_AGENT_ID` unset / bad arguments / invalid name) | Run from inside an agent shell; fix the command. |
| `69` | no daemon (can't reach the browser service) | The service isn't running -- report it; don't blindly retry. |

## Quick recipes

```bash
# Make a browser and start driving.
uv run agentic-browser-fleet new                       # prints the attach line -- run it once
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

# Two browsers, independently (no queueing -- different names).
uv run agentic-browser-fleet new
uv run agentic-browser-fleet new

# Hit a CAPTCHA -- hand it over, then STOP.
uv run agentic-browser-fleet handoff browser-1 "solve the CAPTCHA on the sign-in page"
```

## Don'ts

- **Don't run `playwright-cli close`, `attach` (twice), `detach`, `close-all`, `kill-all` or
  `delete-data`.** The fleet owns lifecycle. Ending a browser is
  `agentic-browser-fleet close <name>`, which also retires the name and deletes the profile.
- **Don't re-attach a browser whose session died** -- see "Crashes" above. It hangs rather than
  erroring, which is the one failure you cannot recover from.
- **Don't interpret a `playwright-cli` failure.** Run `ls`.
- **Don't conclude a scrollable list is complete** because `find` came up empty.
- **Don't `--reclaim` a browser a human is holding** without them explicitly saying so.
- **Don't try to solve a CAPTCHA or 2FA challenge.** Hand it off.
