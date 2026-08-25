---
name: agentic-browser-fleet
description: Own a shared Chromium browser the user can watch and take over, then drive it yourself with playwright-cli. Use when the user wants you to do something on the web (log in somewhere, fill a form, click through a flow, read a page that needs interaction) rather than just fetch a URL.
metadata:
  author: imbue
---

# Driving the browser fleet

Two tools, and the split matters:

- **`agentic-browser-fleet`** (this skill) — *owns* browsers. Start one, list them, hand one to
  the human, give it back. Run from the repo root via `uv run`.
- **`playwright-cli`** — *drives* them. Snapshot the page, click, type, scroll. See the
  `playwright-cli` skill; everything about clicking a button lives there.

```bash
uv run agentic-browser-fleet new
  -> started browser browser-1
     drive it:  playwright-cli -s=browser-1 attach --cdp=http://127.0.0.1:8083/browser-1/<token>
```

Copy that `drive it:` line and run it. From then on it is plain `playwright-cli -s=browser-1 …`.

## First: there are no browsers until you make one

The fleet starts **empty**. `new` prints a **name** (numbered `browser-<N>`, shown as "Browser N"
in the workspace UI) and the attach command. Browsers are addressed by name everywhere.

- `new` mints the first free `browser-<N>`; `new my-browser` chooses the name (lowercase letters
  and digits joined by single dashes; duplicates are rejected, and a *crashed* browser keeps its
  name until you `close` it).
- **The fleet is capped (2 by default).** `new` past the cap returns `2/2 browsers open -- close
  one first`.
- `new` returns as soon as the browser is registered; Chromium is still launching. If the attach
  line says "still starting up", wait a few seconds and run `ls`.
- **Browsers cannot be renamed.** If the user asks, say so: the only option is `close` + `new`,
  which is a different browser with a fresh profile.

## Commands

```bash
uv run agentic-browser-fleet ls                      # the whole fleet: names, owners, tabs
uv run agentic-browser-fleet ls --include-tabs       # every tab of every browser
uv run agentic-browser-fleet new [name]              # start one; prints its name + attach line
uv run agentic-browser-fleet close <name>            # close it, retire the name, DELETE its profile
uv run agentic-browser-fleet acquire <name>          # reserve it (also reprints the attach line)
uv run agentic-browser-fleet acquire <name> --reclaim   # take back from a human -- only if they said so
uv run agentic-browser-fleet release <name>          # hand it back (alias: unlock)
uv run agentic-browser-fleet handoff <name> "reason" # give it to the human (alias: request-human)
```

`ls` shows who controls each browser (`you`, `agent <name>`, `human (took control)`, or `free`).

## The ownership rules, and why you check `ls` before believing anything

Every browser has exactly one controller. **The human always wins, instantly** — the moment they
take control, your very next `playwright-cli` command is refused mid-session.

Here is the part to internalise: **`playwright-cli` exits 1 for every failure and cannot tell you
why.** A revoked lease and a stale element ref look identical. So:

> **On any `playwright-cli` failure, do not interpret the message. Run
> `uv run agentic-browser-fleet ls` and branch on what the fleet says.**

`ls` is the authoritative signal for lost control, a crash, a close, and an expired lease alike.
The fleet's own commands (`acquire` especially) *do* return meaningful exit codes — that is why
you `acquire` before a long stretch of driving.

- **You auto-acquire and hold a sticky lease.** No manual `acquire` needed for normal driving.
- **The lease goes idle after ~60s of no commands.** Holding the CDP connection open is not
  enough — only actual commands count. If you step away and come back, just re-acquire.
- **Release when a browser leaves your active work** (`release <name>`), so control returns to
  the human immediately: task finished, user told you to stop, or you moved to another browser.
- **The human takes over.** Your next command fails. **Stop, tell the user the human took the
  wheel, and end your turn.** Do not retry or `--reclaim` on your own. You are queued to resume
  first and will be messaged; on resume, take a fresh `snapshot` (the page changed, and the view
  may have been resized while they held it, so every ref is stale).
- **Agents never preempt each other.** Another agent's browser is theirs. Use a different one
  (or `new`); `acquire` only if you specifically need that browser and are willing to queue.
- **Never `--reclaim` a browser a human is holding on your own initiative.** End your turn and
  ask. A bare "keep going" from the user counts as confirmation.
- **A browser can crash** (Chromium killed, e.g. out of memory). It is gone for good — `ls` shows
  it crashed. Run `new`; the crashed name stays reserved until you `close` it.
- **Browsers persist across a restart** (tabs, cookies, logins), so a site you logged into
  earlier is probably still logged in. Right after a restart the fleet may be restoring; `ls`
  works, and driving commands may need a few seconds.

## Hitting a wall a human must clear (CAPTCHA / 2FA / login)

CAPTCHA, "verify you're human", an SMS/2FA code you don't have, or a login needing the user's own
credentials: **do not try to solve it yourself** — you will fail and may get the account flagged.

```bash
uv run agentic-browser-fleet handoff browser-1 "solve the CAPTCHA on the sign-in page"
```

`handoff` puts you at the **front** of the resume queue, hands control to the human (pinned, so
it won't pass to another agent), and surfaces the pane. In the **same turn**: tell the user
exactly what to do and on which page, then **end your turn**. You are woken first when they hand
it back — re-`snapshot` to confirm the challenge cleared, then carry on.

## Live view vs. your output

The browser streams to a UI pane next to your chat, and it follows whatever tab you are acting
on. `new` and your first command surface it automatically — but only when the user is currently
watching your chat. Don't manage panes yourself; if the user asks for a browser that isn't
showing, tell them to open it from the workspace **+ → browser** menu.

The pane is **viewer only** — your real output is here in the CLI. Read and relay it; never tell
the user to "check the tab" for results.

## Exit codes -- branch on these

These are the FLEET's exit codes. `playwright-cli` has its own, and they are not this granular.

| Code | Meaning | What to do |
|---|---|---|
| `0` | ok | Carry on. |
| `1` | error | Read the message. |
| `2` | preempted (human took control, or you ran `handoff`) | **Stop and end your turn.** You'll be messaged to resume; re-`snapshot` first. |
| `3` | busy (another agent holds it, or fleet full / still restoring / still launching) | Use a different browser (or `new`); for "restoring"/"starting up", wait a few seconds and retry. |
| `4` | timed out waiting for another agent | Try later, or pick a different browser. |
| `64` | usage (`MNGR_AGENT_ID` unset / bad arguments / invalid name) | Run from inside an agent shell; fix the command. |
| `69` | no daemon (can't reach the browser service) | The service isn't running -- report it; don't blindly retry. |

## Sub-agents

Drive the browser **yourself, in this chat**. A `launch-task` sub-agent runs in a separate,
isolated container with no access to this workspace's fleet. If a sub-agent needs something from
the web, have it tell you what it needs and you do the browsing.
