# system-monitor

The Memory & disk app: one page that shows how much memory and disk this
workspace is using, refreshed every five seconds.

- **Memory**: used out of total, a note from 80% used (the memory guard
  starts stopping programs below 10% free) naming the largest kind of memory
  the user can free, or when the memory guard stopped something in the last
  24 hours, and a graph of memory in use over the last 10 minutes (0 to 100
  percent). The app itself records a reading every 5 seconds while it runs
  (`memory.MemoryHistory`, one read of the memory totals each) and sends the
  history with every update, so the line stays whole while a window is hidden
  or reloaded. The history lives in the app, so it starts over when the app
  restarts; a stretch with no readings (the machine paused) is left blank.
- **What's using memory** (`usage`): chats on any agent type (claude, codex,
  pi), each with whatever it is running, found through oom_priority's
  agent-pid registry; code checkers; the workspace's own programs (chat app,
  desktop, built-in apps, background services); and the apps the user built.
  - Where the kernel reports each program's own and proportional memory
    (`/proc/<pid>/smaps_rollup`), each row holds its programs' private memory,
    about what stopping them frees. Memory several programs map (the Python
    and Node runtimes, Chromium, system libraries) is counted once in a Shared
    row, the files in the scratch space (`/tmp`) are a Temporary files row, and
    the rest of the memory in use (the kernel, programs this app cannot
    inspect) is a System row. The rows add up to the memory in use
    (`usage.exact_usage`).
  - Where it does not (gVisor), only resident sizes are available, and each is
    scaled down by one factor until the rows add up to what is in use less the
    scratch space (`usage.scaled_usage`). The page then says the values are
    approximate.
  - The Temporary files row appears only where `/tmp` is a tmpfs (every workspace
    today), since only then are its files held in memory; it shows its size
    limit, and an info popup explains what it is.
- **Disk**: the main disk the workspace lives on.
- **Programs stopped to free memory** (`stops`): each stop the memory guard
  (earlyoom, see `system/services/oom_priority/`) recorded in the last 24
  hours: when, and what it was in plain words, with the exact command behind a
  Details toggle. The description and command come from pairing each record in
  the shed ledger with the guard's own log line for it; a stop the log no
  longer has keeps a plainer description and no Details.

The data comes from `/proc`, supervisord's control socket, the app registry,
the chat records under `~/.mngr/agents/`, oom_priority's agent-pid registry and
shed ledger, and earlyoom's log (`programs` and `stops`). The app writes
nothing to disk; it keeps only the memory graph's last 10 minutes and the
latest snapshot, in its own memory.

The app keeps its own overhead low: running programs come from supervisord's
control socket, with no `supervisorctl` process started, every open window
shares one snapshot taken at most every 4 seconds (`stats.SharedSnapshot`), the
stop history and the guard's log are read only from their last 256 KB and 1 MB
and only again once they change (on a stop), the page is sent only the fields
it shows, and a hidden window stops polling.

Stops are paired with the guard's log lines newest first, on pid and process
name. The log is rotated and the ledger is not, so the oldest stops lose their
lines. Taken in that order they find none, so a later stop's line that reused
their pid can never mislabel them; they keep a plainer description with no
command.

Two known limits:

- A pid in the agent-pid registry is trusted while a process holds it. If an
  agent exits and an unrelated process reuses its pid before the next agent
  launch prunes the registry, that process counts as the agent's chat.
- Code checkers are recognised by command-line fragments of the common language
  servers (`programs.CODE_CHECKER_MARKERS`); one not in that list counts under
  the chat or program that started it.

The app describes its own HTTP API at `GET /api/schema` (the manifest's
`api_schema_path`), an OpenAPI document built from its routes by
`system/libs/app_schema`.

Served on port 8040 by the `system-monitor` program.
