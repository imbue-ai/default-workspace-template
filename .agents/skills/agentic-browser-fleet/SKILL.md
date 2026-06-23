---
name: agentic-browser-fleet
description: Drive a fleet of shared Chromium browsers by handing high-level goals to autonomous browser-use agents. Use when the user wants you to do something on the web (log in somewhere, fill a form, scrape a page, click through a flow) rather than just fetch a URL. You orchestrate via the `agentic-browser-fleet` CLI; you never click pages yourself.
---

# Driving the browser fleet

There are **two** agents in this picture, and keeping them straight is the
whole game:

- **You (the orchestrator).** The Claude Code agent reading this. You never
  touch a page, never click, never type into a field. You run
  `agentic-browser-fleet` commands. That is your *entire* interface to the
  browser.
- **The browser-use agent.** When you run `task <id> "<prompt>"`, the daemon
  spins up an autonomous *browser-use* agent on that browser. *It* does the
  clicking, typing, and navigating to accomplish the goal you handed it. It
  streams its reasoning back to you as `[thinking] ...` / `[action] ...`
  lines on the CLI's stdout -- i.e. into *your* output, where the user can
  read it.

"Take control" is a **human** action in the UI. The agent never invokes it.
If you ever feel like you want to "take control," you are confused -- you
drive by issuing another `task`, not by grabbing the wheel.

The CLI is a thin HTTP client to a per-workspace browser daemon. It is
stateless: each command opens a connection, does its thing, and exits.

Run it from the repo root via `uv run`:

```bash
uv run agentic-browser-fleet <command> ...
```

It requires `MNGR_AGENT_ID` in the environment (it is set automatically
inside an agent shell). If it is missing, the CLI exits `64` telling you to
run it from inside an agent.

## The mental model in one paragraph

You give a browser a *goal*, not a *script*. `task 0 "log into example.com
and download last month's invoice"` hands that sentence to the browser-use
agent on browser 0; the agent figures out the clicks. You watch its trace
scroll by in your own output and you read its final `done:` line. The browser
tab that pops into the UI is a **viewer** for the human -- the trace is in
*your* stdout, not in the tab.

## Commands

### `ls` -- see the fleet

```bash
uv run agentic-browser-fleet ls
```

```
browser 0: you -- 2 tab(s), active: https://example.com/invoices
browser 1: agent alice -- 1 tab(s), active: https://news.example.com
browser 2: human (took control) -- 1 tab(s), active: https://bank.example.com
browser 3: free -- 1 tab(s), active: (no tab)
```

Each line shows the browser id, who controls it (`you`, `agent <name>`,
`human (took control)`, or `free`), tab count, and the active tab's URL. If
there are no browsers yet it tells you to use `new` or `task 0`.

### `new` -- start a browser

```bash
uv run agentic-browser-fleet new
# -> started browser 4
```

Prints the new browser's id. You usually do not need this -- `task <id>` on a
not-yet-existing default browser (`task 0`) is the common entry point. Exits
`3` if the daemon refuses to start another (e.g. a fleet cap).

### `task <id> "<prompt>"` -- the workhorse

```bash
uv run agentic-browser-fleet task 0 "go to news.ycombinator.com and give me the titles of the top 5 stories"
```

This is what you'll use 95% of the time. It:

1. Pulls browser 0 into a UI pane next to your chat (so the human can watch).
2. **Acquires** the browser (waiting in a FIFO queue if another agent holds
   it -- see Ownership below).
3. Hands your prompt to the browser-use agent and streams its trace:

   ```
   (working on browser 0)
   [thinking] I need to navigate to the Hacker News front page.
   [action] navigate https://news.ycombinator.com
   [thinking] Reading the story titles from the listing.
   [action] extract top 5 story titles
   done: 1. ... 2. ... 3. ... 4. ... 5. ...
   ```

4. **Releases** the browser automatically when it finishes. *The connection
   is the lease* -- when `task` exits (success, error, or you Ctrl-C it), the
   browser frees. This is why you normally never call `lock`/`unlock`
   yourself.

Read the final line: `done: <result>` is the answer. Relay it to the user.

Flags:

| Flag | Effect |
|---|---|
| `--reclaim` | Resume a browser a **human** took control of. Use ONLY when the human told you to (see Ownership). Never on your own. |
| `--no-wait` | If another agent holds the browser, fail fast (`exit 3`) instead of queueing behind them. |
| `--max-wait S` | Wait at most `S` seconds in the queue for another agent to release, then give up (`exit 4`). Default: wait indefinitely. |
| `--no-pane` | Do not pull the browser into a UI pane. Use for headless/background work the human isn't watching. |

### `lock <id>` / `unlock <id>` (alias `release <id>`)

`lock` holds a browser in the foreground until you Ctrl-C (it prints
`holding browser <id> (Ctrl-C to release)` and blocks). `unlock`/`release`
frees a browser you hold.

**You rarely need these.** `task` already acquires-and-releases around each
goal. Reach for `lock` only when you must keep one browser reserved across
*several* separate `task` invocations and cannot tolerate another agent
slipping in between them. The same queueing/`--no-wait`/`--max-wait`
semantics apply to `lock`.

```bash
# Reserve browser 1 across a multi-step sequence, then free it.
uv run agentic-browser-fleet lock 1        # blocks, holding it (Ctrl-C to free)
# ... in another shell, run several `task 1 ...` while the lock is held ...
uv run agentic-browser-fleet release 1     # or just Ctrl-C the lock
```

`release` on a browser that wasn't yours prints `browser <id> was not yours
to release` and still exits 0.

## Ownership (read this carefully)

Every browser has **exactly one** controller at a time. The rules the daemon
enforces, and how you must react:

1. **`task`/`lock` acquire it; ending the command releases it.** No manual
   bookkeeping in the normal case.

2. **Agents never preempt each other.** If another agent holds the browser,
   `task` does **not** barge in -- it **waits in a FIFO queue** and streams:

   ```
   browser 1 is busy (agent alice) -- waiting for it to free up...
   ```

   When alice's connection drops, the queue advances and your task runs. Use
   `--no-wait` to fail fast instead, or `--max-wait S` to bound the wait.

3. **A human can take control at any time from the UI.** If a human takes
   control *while your task is running*, your task **ends immediately**:

   ```
   lost control of browser 1 (you took over). Send me a message
   ("keep going", "resume", whatever) when you want me to continue.
   ```

   This is **exit code 2 (preempted)**. Do **NOT** retry automatically. Tell
   the user something like: *"You took over browser 1 -- say 'keep going'
   when you want me to resume."* Then stop and wait for them.

4. **Resuming after a human took control requires `--reclaim` -- and explicit
   permission.** Only when the human *explicitly* tells you to resume
   ("keep going" / "resume" / "take it back") do you re-run the same task
   with `--reclaim`:

   ```bash
   uv run agentic-browser-fleet task 1 "<the original goal, or where to pick up>" --reclaim
   ```

   **NEVER pass `--reclaim` on your own initiative.** Reclaiming yanks the
   browser back from a human who is actively using it -- that is grabbing the
   wheel. `--reclaim` is *only* ever a direct response to "keep going."

5. **Starting a task on a human-held browser without `--reclaim` fails.** You
   get `exit 3` and:

   ```
   browser 1 is under human control. It is yours to drive; when you are done,
   click "Return to agents" (or tell me to resume and I will reclaim it).
   ```

### Exit codes -- branch on these

| Code | Name | Meaning | What to do |
|---|---|---|---|
| `0` | done | Task completed (or `lock`/`release` succeeded). | Read the `done:` line; relay the result. |
| `1` | error | The browser-use agent or daemon errored. | Read the `error:` line. Fix the prompt or report the failure; don't blindly retry. |
| `2` | preempted | A human took control mid-task. | Stop. Tell the user you lost control; resume only on their say-so (with `--reclaim`). Do NOT auto-retry. |
| `3` | busy | Held by a human, or held by another agent and you passed `--no-wait`, or `new` was refused. | Human-held: ask the user (then `--reclaim` if they agree). Agent-held: re-run without `--no-wait` to queue. |
| `4` | timed-out | You passed `--max-wait` and another agent still held it when time ran out. | Try again later, queue without `--max-wait`, or pick a different browser. |
| `64` | usage | `MNGR_AGENT_ID` unset / bad arguments. | Run from inside an agent shell; fix the command. |
| `69` | no daemon | Can't reach the browser daemon. | The browser service isn't running -- report it; this isn't something to retry blindly. |

## The UI pane

Any browser you `task` (or `lock`) is automatically pulled into a split pane
to the right of your chat via `scripts/layout.py`, so the human can watch the
browser-use agent work in real time. Pass `--no-pane` to skip this for
headless background work.

Crucially: **the tab is viewer-only.** The agent's `[thinking]`/`[action]`
trace and the final `done:` result appear in **this CLI's stdout** (your
output), *not* inside the browser tab. So always read and relay the CLI
output -- don't tell the user "check the tab" for results. The tab is just
the live picture; the words are in your stream.

If pulling the pane fails (e.g. layout unavailable), it's non-fatal: the
browser still runs and you'll see a one-line warning. The task proceeds
regardless.

## Sub-agents that use browsers

If you delegate to a sub-agent (via the `launch-task` skill) and that
sub-agent will run `agentic-browser-fleet` itself, its browser panes should
open next to **your** chat -- where the human is watching -- not buried by the
sub-agent's own (often unwatched) chat.

The mechanism: `_pull_in_pane` reads `BROWSER_FLEET_ANCHOR` and, if set,
splits the new browser pane *relative to that anchor* (`scripts/layout.py
split <ref> --relative-to <anchor> --direction right`). The anchor is a chat
ref of the form `chat:<agent-name>`. So set it to **your own** chat ref in the
sub-agent's environment:

```
BROWSER_FLEET_ANCHOR=chat:<your MNGR_AGENT_NAME>
```

`launch-task`'s `create_worker.py` has **no** flag for injecting arbitrary
env vars into the worker. The way to get a value into the sub-agent's
environment is through the **task file**: in the task body you write for the
sub-agent (the human-readable instructions), tell it to export the anchor
before any browser work, e.g.:

```
## Environment
Before running any `agentic-browser-fleet` command, export:

    export BROWSER_FLEET_ANCHOR=chat:<your-orchestrator-name>

so the browser panes open next to the orchestrator's chat (where the human
is watching), not next to yours.
```

(Substitute your actual `MNGR_AGENT_NAME` for `<your-orchestrator-name>`.)

If the anchor is unset it still works fine -- the sub-agent's browser pane
just opens next to its own chat instead. The anchor is an
ergonomics-for-the-human optimization, not a correctness requirement.

## Working several browsers at once

Each `task` connection is independent, so concurrency is just "run more than
one `task`." Background them and collect results. Use distinct browser ids so
they don't queue behind each other:

```bash
# Three browsers working in parallel (Bash run_in_background: true on each).
uv run agentic-browser-fleet task 0 "summarize the front page of site-a.com"
uv run agentic-browser-fleet task 1 "check whether site-b.com is up and what it says"
uv run agentic-browser-fleet task 2 "grab the latest headline from site-c.com"
```

If you instead point two `task`s at the *same* id, the second waits in the
FIFO queue for the first to finish (that's the ownership rule, not a bug). One
goal per browser at a time; spread work across ids for true parallelism.

## Quick recipes

```bash
# One-off: do a thing, get the answer.
uv run agentic-browser-fleet task 0 "what's the current price shown on example.com/pricing for the Pro plan?"

# Don't wait if someone else is on it; just tell me it's busy.
uv run agentic-browser-fleet task 1 "..." --no-wait

# Wait up to 30s for another agent to free browser 1, then give up.
uv run agentic-browser-fleet task 1 "..." --max-wait 30

# Headless background job the human isn't watching (no pane).
uv run agentic-browser-fleet task 2 "..." --no-pane

# Human took control, then said "keep going" -- and ONLY then:
uv run agentic-browser-fleet task 1 "continue where we left off: submit the form" --reclaim
```

## Don'ts

- Don't try to click/type/navigate yourself. Hand a goal to `task`; the
  browser-use agent does the interaction.
- Don't "take control" -- that's a human-only UI action.
- Don't pass `--reclaim` unless the human explicitly told you to resume a
  browser they took over.
- Don't auto-retry on exit `2` (preempted). Stop and wait for the human.
- Don't tell the user to "look in the tab" for results -- the trace and the
  answer are in the CLI output you're already reading.
