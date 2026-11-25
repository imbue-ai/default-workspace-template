---
name: workspace-memory
description: Use for any question about this workspace's memory -- how much memory something uses (a chat, an app, the browser, a command), what is using it, what could be stopped to free some, the workspace running low or slowing down, or a command, chat or agent the memory guard stopped ("shed"). Measures memory the way the memory guard does, and frees it only with the user's approval.
metadata:
  author: imbue
---

# Workspace memory

The workspace has a fixed amount of memory, and a memory guard (earlyoom) stops the most
expendable processes when it runs low. Measure before you answer anything about memory, and
measure the way that guard does: the usual Linux tools give numbers that look precise and are
several times too high here.

## Measuring

1. **Start with the memory candidates command.**

   ```bash
   python3 system/services/oom_priority/bin/memory_candidates.py          # a table
   python3 system/services/oom_priority/bin/memory_candidates.py --json   # the same report as JSON
   ```

   Its first line is the free memory **the guard decides on**, and how much of it is left before
   the guard starts stopping processes (it acts at a share of the total, not at zero). Then it
   lists what could be stopped: idle chats and workers, the apps the workspace may stop, and the
   browser while no window shows the Screen. Last, under "Also holding memory, not candidates", it
   lists everything else that holds memory with why it is not offered: a chat mid-turn or in use,
   the main chat, a critical app, a workspace service, the browser while a window shows it. Every
   row carries the same figure, the memory its processes made for themselves, which is roughly what
   stopping it frees. A section it could not read says "unknown", which is not "nothing".
   `--free-memory` prints only the first line and is cheap enough to repeat while memory is short.
   `system/services/oom_priority/README.md` ("Memory candidates") has the details.

2. **For the breakdown the user sees**, ask the System Monitor app, whose page shows the same
   rows: `curl -s http://127.0.0.1:8040/api/stats`. Its `usage` rows are grouped as the page groups
   them (chats, code checkers, the workspace's own programs, the user's apps, Shared, Temporary
   files, System) and add up to the memory in use. When `usage_mode` is `scaled` the rows are
   approximate, and say so. The app stops itself a minute after its last window closes; a request
   then answers with a "Starting" page and wakes it, so ask again a second later.

3. **For one process**, add up its `Anonymous` lines:

   ```bash
   awk '/^Anonymous:/ {s += $2} END {print s " kB"}' /proc/<pid>/smaps
   ```

   (`smaps_rollup` gives the same sum in one line where it exists; gVisor has none.) For a group
   of processes, sum that over each of them.

### What not to measure with

Never sum `ps` RSS (`VmRSS`) or `top`'s RES, and never judge free memory by `free` or
`/proc/meminfo`. A resident size counts every file the process maps, so a sum over processes
counts each shared file once per process. PSS, which splits shared pages on a plain Linux kernel,
does not help under gVisor (cloud workspaces run under it; AGENTS.md, "Sandboxed runtime"): there
every page a process maps reads as its own, and `Pss` equals `Rss`.
Chromium maps a binary of about 400 MB into every one of its processes, so summing their resident
sizes counts it once per process: one agent added up Chromium's `ps` RSS and reported 2.7 GB for a
browser whose own memory was well under 1 GB. The container's `/proc/meminfo` is wrong the other
way: under a memory cap it misses what the runtime is charged and has read three times the real
headroom. The candidates command reads the guard's own figure; use its.

### Telling the user

Give the figures as "about": pages a process shares with its parent after a fork count in both,
so a browser can read somewhat above what stopping it frees. Name each thing as the user knows
it (the chat's title, the app's name, "the browser"), never by process or program name. After
anything is stopped, run the command again: the free memory it reports then is the real answer.

## Freeing memory

When the memory guard sheds something and an agent reports it to you -- a worker's `question`
gate about a shed test command, a worker you find shed (`lead-proxy.md`,
`dead-worker-recovery.md`), or anything else an agent or the user brings you -- the workspace ran
short of memory, and the next heavy command is likely to be shed too. The same holds when the
user asks you to free memory. Memory is the user's to spend, so you find what could be freed and
let them choose; you never stop anything on your own.

1. **List the candidates** with the command above. A chat or agent that ended its turn to wait on
   a background task is not idle -- the task will wake it, and stopping it would lose the work --
   so it is never a candidate, and neither is the main chat, which comes straight back when it is
   stopped. It only reads; it stops nothing.

2. **Offer them to the user.** In plain language, say that the workspace ran low on memory and
   something was paused (when that is what happened), how much memory is free, and each candidate
   with what it is, when it was last used, and how large it is next to the others. Say that a
   stopped chat comes back by itself the next time they send it a message. Ask which, if any, to
   stop. Never pick for them, and never offer the chat they are talking to you in.

3. **Stop only what they approve**, each the way the README's "Memory candidates" section says (a
   chat with `mngr stop`, a background agent with the launcher's `stop`, a browser through the
   Screen service, an app with `supervisorctl stop <program>`), then run the candidates command
   again and tell them what is free now.

4. **Answer whoever reported the shed.** For a worker waiting on a `question` gate, reply that it
   can rerun, or to wait if nothing was freed and memory is still falling. If the user declines to
   stop anything and memory stays low, tell them plainly that the checks are paused until memory
   frees up; do not tell the worker to skip them, since memory pressure is never a reason to skip a
   test gate.

Run this once per shed report or request, not on a schedule: listing is cheap, but every offer is a
question the user has to stop and answer.

One flow does not ask: a workspace update frees memory on its own judgment, and a scheduled one may
go further than this skill allows. Its rule is
`.agents/skills/update-self/references/freeing-memory-for-an-update.md`, and it is that flow's
alone.
