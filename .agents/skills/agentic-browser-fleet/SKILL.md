---
name: agentic-browser-fleet
description: Drive a fleet of shared Chromium browsers yourself, one command at a time, from your shell. Use when the user wants you to do something on the web (log in somewhere, fill a form, click through a flow, read a page that needs interaction) rather than just fetch a URL. YOU run the `agentic-browser-fleet` commands, look at the page, decide what to click, and click it -- in this same chat, with your own reasoning.
---

# Driving the browser fleet

You operate the browser by running `agentic-browser-fleet` commands directly: ask the page what's on it, decide what to do, do it, look again. Run every command from the repo root via `uv run`:

```bash
uv run agentic-browser-fleet <command> ...
```

## The loop

1. `state <id>` -- prints the page as a numbered list of clickable elements.
2. Read it, decide which element you want.
3. Act: `click <id> <index>` (or `input` / `select` / `scroll` / `keys` / `open`).
4. `state <id>` again to see what changed. Repeat.

Worked end to end:

```text
uv run agentic-browser-fleet open 0 https://example.com      -> ok: navigate
uv run agentic-browser-fleet state 0
  browser 0 @ https://example.com/  (Example Domain)
  Example Domain
  This domain is for use in illustrative examples...
  [18]<a /> Learn more
uv run agentic-browser-fleet click 0 18                      -> ok: click
uv run agentic-browser-fleet state 0     # re-state: page is now iana.org
  browser 0 @ https://www.iana.org/help/example-domains  (Example Domains)
  ...
```

Every clickable thing gets a `[number]`; that number is what you pass to `click` / `input` / `select`.

### Requery before you act

The `[number]` indices come from the **latest** `state` and are **ephemeral** -- the page re-numbers its elements whenever it changes. So:

- **Always `state <id>` before you `click`.**
- **Re-run `state <id>` after every `open`, `click`, `select`, `scroll`, or `tab`, and after you regain control of a browser.**

If you click against a stale index the CLI refuses rather than mis-clicking:

```text
uv run agentic-browser-fleet click 0 18
  that element index is stale -- run `state 0` again first      (exit 1)
```

Treat that as "look first": run `state 0`, find the element under its new number, click that.

### No API key needed

`state` / `open` / `click` / `input` / `select` / `scroll` / `keys` / `screenshot` / `tab` are deterministic mechanical operations -- no LLM, no API key. (Only the optional `task` fallback at the end uses an LLM and needs a key.)

## Commands

Every command's **first argument is the browser id** (`0` is the default browser). The fleet-level commands `ls` and `new` are the exception.

### Picking and making browsers

```bash
uv run agentic-browser-fleet ls
```

```text
browser 0: you -- 2 tab(s), active: https://example.com/invoices
browser 1: agent alice -- 1 tab(s), active: https://news.example.com
browser 2: human (took control) -- 1 tab(s), active: https://bank.example.com
browser 3: free -- 1 tab(s), active: (no tab)
```

`ls` shows the whole fleet: each browser's id, who controls it (`you`, `agent <name>`, `human (took control)`, or `free`), tab count, and active tab URL -- so you can pick one.

- `ls --include-tabs` lists every tab of every browser:

  ```text
      [0]* Invoices            https://example.com/invoices
      [1]  Dashboard           https://example.com/home
  ```

- `new` starts another browser and prints its id (`-> started browser 4`).
- `close <id>` closes an entire browser (all its tabs) and retires its id (never reused). Use when permanently done with a browser. For a single tab, use `tab <id> close`.
- The fleet is **capped (5 by default)**. `new` past the cap returns `Too many open browsers (5/5)` -- `release` or `close` one you're done with first.
- If there are no browsers yet, `ls` says so; running any command on browser 0 (e.g. `state 0`) starts it.

### Looking at the page

```bash
uv run agentic-browser-fleet state 0
```

Prints `browser 0 @ <url>  (<title>)`, a `tabs:` line if more than one tab is open, then the numbered interactive elements. A page with none prints `(no interactive elements -- try screenshot)`. This is your eyes -- run it constantly.

```bash
uv run agentic-browser-fleet screenshot 0
# -> screenshot saved: /path/to/shot.png  (Read it to view)
```

`screenshot` saves a PNG and prints its path. **Read that path** with your Read tool to see the page -- use it for visual layouts, canvas, charts, captchas, or anything `state`'s text list can't convey.

### Acting on the page

| Command | What it does |
|---|---|
| `open <id> <url>` | Navigate the browser to a URL. (`-> ok: navigate`) |
| `click <id> <index>` | Click the element with that index from the last `state`. (`-> ok: click`) |
| `input <id> <index> "text"` | Type text into the field at that index. (`-> ok: input`) |
| `select <id> <index> "value"` | Choose an option in a `<select>` dropdown by its visible text. (`-> ok: select`) |
| `scroll <id> [down\|up] [--amount N]` | Scroll the page. Direction defaults to `down`; `--amount` is pixels (default 500). |
| `keys <id> "Enter"` | Send keyboard keys, e.g. `"Enter"`, `"Control+a"`, `"Tab"`. |

A typical fill-and-submit:

```bash
uv run agentic-browser-fleet state 0                          # find the field indices
uv run agentic-browser-fleet input 0 5 "alice@example.com"    # email field
uv run agentic-browser-fleet input 0 6 "hunter2"              # password field
uv run agentic-browser-fleet click 0 7                        # the "Log in" button
uv run agentic-browser-fleet state 0                          # re-state: landed on the dashboard?
```

(Or `keys 0 "Enter"` to submit the focused form instead of clicking the button.)

### Tabs within one browser

```bash
uv run agentic-browser-fleet tab 0 list           # list this browser's tabs
uv run agentic-browser-fleet tab 0 new --url https://example.com/help
uv run agentic-browser-fleet tab 0 switch 1       # make tab index 1 active
uv run agentic-browser-fleet tab 0 close 2        # close tab index 2
```

`tab` with no action defaults to `list`. After `switch` / `new` / `close` the active page changed, so **`state <id>` again** before clicking.

### Ownership commands

```bash
uv run agentic-browser-fleet acquire 0            # reserve browser 0 across commands
uv run agentic-browser-fleet acquire 0 --reclaim  # take it back from a human -- ONLY on their say-so
uv run agentic-browser-fleet release 0            # let it go (alias: unlock 0)
uv run agentic-browser-fleet handoff 0 "solve the CAPTCHA"  # hand to the human (alias: request-human)
```

You usually don't need `acquire`: your first command on a browser auto-acquires it and you keep a sticky lease across the commands that follow. Use `acquire` to explicitly reserve a browser, or to queue behind / reclaim one that's held. `release` (alias `unlock`) hands it back; releasing one that wasn't yours prints `browser <id> was not yours to release` and still exits `0`.

## Ownership rules

Every browser has exactly one controller; every command's output names the owner.

- **You auto-acquire and hold a sticky lease.** No manual `acquire` needed for normal driving.
- **Release when a browser leaves your active work** (`release <id>`), so control returns to the human immediately rather than after the ~90s idle timeout:
  - Task finished on that browser -> release it.
  - User tells you to stop -> release every browser you were driving.
  - You switch to a different browser for the rest of the task -> release the one you're leaving.
  - Driving several at once -> keep them until fully done, then release each.
  - If you forget, an idle lease auto-frees after ~90s; if a later command says you no longer hold it, just acquire it again.
- **The human always wins.** If a human takes control, your next command comes back with status `busy_human`/`lost_control` (exit 2). You lost control: **stop, tell the user the human took the wheel, and end your turn.** Do not retry, poll, or `--reclaim` on your own. You're queued to resume first; you'll be messaged when they hand it back. On resume, **re-run `state <id>` first** (the page changed), then continue. Resume early only on an explicit "keep going": `acquire <id> --reclaim`, then `state <id>`.
- **Agents never preempt each other.** A browser another agent holds returns (exit 3):

  ```text
  browser 1 is held by another agent -- you're queued for it...
  ```

  Default to a **different** browser (or `new`) for unrelated work; only `acquire 1` to queue when you specifically need that browser. (Contrast with a human taking *your* browser: there your work is on it, so you wait and resume the same one.)
- **A browser can crash.** If Chromium is killed, your next command on it returns (exit 1):

  ```text
  browser 0 crashed (Chromium was killed -- e.g. out of memory) and is gone.
  Start a fresh one with `new` (it gets a new number).
  ```

  That browser is gone for good -- don't retry or re-`state` it; run `new` and carry on there.
- **Browsers persist across a restart** (browsers, tabs, cookies/logins/history are saved), so a site you logged into earlier is probably still logged in. Right after a restart the fleet is restoring; a state-changing command may briefly return (exit 3):

  ```text
  the browser fleet is still starting up (restoring your saved browsers) -- try again in a few seconds.
  ```

  Wait a moment and retry (`ls` and `state` work during this window).

## Hitting a wall a human must clear (CAPTCHA / 2FA / login)

When you hit a CAPTCHA, reCAPTCHA / hCaptcha / Cloudflare "verify you're human" challenge, an "I'm not a robot" checkbox, an SMS / 2FA / OTP code you don't have, or a login needing the user's own credentials -- **do not try to solve it yourself** (you'll fail and may get the account flagged). Hand it off:

```bash
uv run agentic-browser-fleet handoff 0 "solve the CAPTCHA on the sign-in page"
```

`handoff` (alias `request-human`) puts you at the **front** of that browser's resume queue, hands control to the human (pinned -- won't pass to another agent), and surfaces the pane so they can see it. In the **same turn**: tell the user exactly what to do and on which page, then **end your turn** (exit 2). You're woken first when they hand control back -- **re-run `state 0`** to confirm the challenge cleared, then carry on.

## Live view vs. your output

The browser shows up live in a UI pane next to your chat so the human can watch you operate it. That pane is **viewer only** -- your actual output (the `state` listings, the `ok:`/error lines, the screenshot paths) is in your CLI output here in the chat. Read and relay the CLI output; don't tell the user to "check the tab" for results.

## Multiple browsers, tabs, sub-agents

- **Multiple browsers:** use different ids; they're independent and don't queue against each other. Drive several at once just by varying the id.
- **Tabs:** `tab <id> ...` manages tabs within one browser.
- **Drive the browser yourself, here in this chat.** A `launch-task` sub-agent runs in a separate, isolated container with no access to this workspace's browser fleet, so do web/browser work yourself. If a sub-agent needs something from the web, have it tell you what it needs and you do the browsing. (A parent passing its chat to a sub-agent can set `BROWSER_FLEET_ANCHOR` so panes anchor to that chat, but the daemon/fleet still isn't reachable from an isolated sub-agent.)

## Exit codes -- branch on these

| Code | Meaning | What to do |
|---|---|---|
| `0` | ok | Read the output; for `state`, decide your next action. |
| `1` | error / stale index / crashed browser | Stale index: `state <id>`, find the new number, retry. Crashed: `new`. Else read the message. |
| `2` | preempted (human took control, or you ran `handoff`) | **Stop and end your turn.** Tell the user; you'll be messaged to resume (re-run `state <id>` first). Don't poll or `--reclaim` on your own. |
| `3` | busy (another agent holds it, or fleet full / still restoring) | Use a different browser (or `new`); you're queued and will be messaged when it frees. For "restoring", wait and retry. |
| `4` | timed out (waited via `--max-wait` and another agent still held it) | Try later, or pick a different browser. |
| `64` | usage (`MNGR_AGENT_ID` unset / bad arguments) | Run from inside an agent shell; fix the command. |
| `69` | no daemon (can't reach the browser service) | The service isn't running -- report it; don't blindly retry. |

## Quick recipes

```bash
# Look, then act.
uv run agentic-browser-fleet state 0
uv run agentic-browser-fleet click 0 12
uv run agentic-browser-fleet state 0            # always re-state after acting

# Read a page by eye when the text list isn't enough.
uv run agentic-browser-fleet open 0 https://example.com/pricing
uv run agentic-browser-fleet screenshot 0       # then Read the printed PNG path

# Search and submit with the keyboard.
uv run agentic-browser-fleet open 0 https://news.ycombinator.com
uv run agentic-browser-fleet state 0
uv run agentic-browser-fleet input 0 3 "browser automation"
uv run agentic-browser-fleet keys 0 "Enter"
uv run agentic-browser-fleet state 0

# Two browsers, independently (no queueing -- different ids).
uv run agentic-browser-fleet open 0 https://site-a.com
uv run agentic-browser-fleet open 1 https://site-b.com

# Hit a CAPTCHA -- hand it to the user, then STOP.
uv run agentic-browser-fleet handoff 0 "solve the CAPTCHA on the sign-in page"
# -> tell the user what to do, end your turn; you resume first when they hand back.
uv run agentic-browser-fleet state 0            # (on resume) confirm the challenge cleared

# Human took over, then said "keep going" -- and ONLY then:
uv run agentic-browser-fleet acquire 1 --reclaim
uv run agentic-browser-fleet state 1            # re-state after regaining control
```

## Fallback only: `task <id> "<goal>"`

When a page is genuinely beyond step-by-step control -- a `<canvas>` app, a drag-heavy visual editor, a flow where `state` shows nothing useful even with a screenshot -- you can hand the whole goal to an autonomous browser-use agent instead of driving it yourself:

```bash
uv run agentic-browser-fleet task 0 "log into example.com and download last month's invoice"
```

This streams the agent's `[thinking]`/`[action]` trace into your output and ends with a `done:` line you relay. It **uses an LLM and needs an API key**, and it takes the wheel away from your direct control for its duration. Flags: `--reclaim` (resume a human-held browser, same rules as above), `--no-wait` (fail fast instead of queueing behind another agent), `--max-wait S` (bound the queue wait, then exit `4`), `--no-pane` (don't pull it into a UI pane). **Prefer driving it yourself** -- reach for `task` only when direct control truly can't see or manipulate the page.

## Don'ts

- Don't `click <index>` without a fresh `state` first -- indices go stale the moment the page changes.
- Don't "take control" -- that's a human-only UI action. You drive by issuing commands.
- Don't pass `--reclaim` unless the human explicitly told you to resume a browser they took over.
- Don't auto-retry on exit `2` (preempted). Stop and wait for the human.
- Don't tell the user to "look in the tab" for results -- your CLI output is the source of truth; the tab is just the live picture.
- Don't jump to `task` for ordinary pages. Drive them yourself.
