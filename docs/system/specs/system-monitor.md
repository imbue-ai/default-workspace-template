# System Monitor: CPU, memory and disk in one app

Status: agreed design (2026-10-20), implemented on `mngr/fixup-system-monitor`.
Audience: implementers of the system monitor app (`system/apps/system_monitor`), the memory guard package (`system/services/oom_priority`), the supervisord drop-ins, and the manifest contract tables.

The Memory & disk app becomes System Monitor: it adds CPU, keeps its memory and disk pages, and opens each resource's own view from an overview.
This spec covers two pieces:

- **Part A: the resource sampler.** A small always-on program beside the memory guard that records overall CPU and memory every few seconds, keeps a day of history, and records "busy stretches" with who caused them.
- **Part B: the app.** The overview with three tiles, the per-resource views, live CPU per chat and app while a window is open, an on-demand disk breakdown, and a backups summary.

## 1. Background

Facts this design builds on, as of `main` at `2cf9763de7` (2026-10-20).

- The app (`system/apps/system_monitor`) is one plain HTML page with no build step, polling `/api/stats` every 5 s; every open window shares one snapshot taken at most every 4 s (`stats.SharedSnapshot`).
  The snapshot reads every process's `/proc/<pid>/status`, `cmdline` and `smaps_rollup`, supervisord's control socket, the app registry, the chat records, the memory guard's agent-pid registry, its shed ledger and the guard's log.
  The app writes nothing to disk and keeps only the memory graph's last 10 minutes, sampled by a thread of its own, so the graph starts over whenever the app restarts, and the shell stops the app a minute after its last window closes (`stop_when_no_windows = true`).
- Measured in a local gVisor container (`runsc`, `--cpus=2`) on 2026-10-20: `/sys/fs/cgroup/cpu.max` and `cpu.stat` do not exist, `/proc/stat` reports every CPU counter as 0, `/proc/loadavg` reports `0.00 0.00 0.00`, and `os.cpu_count()` reports the quota (2).
  Per-process counters are real: `/proc/<pid>/stat` fields 14 to 17 (`utime`, `stime`, `cutime`, `cstime`) moved by the expected amounts for a 1 s own burn (93 ticks) and a reaped 2 s child (187 child ticks), at `CLK_TCK` 100.
  Under runc the same container has `cpu.max` (`200000 100000`), a moving `cpu.stat`, a real `/proc/stat` and load average.
  So inside the sandbox the only CPU accounting that exists everywhere is per process, and the kernel-side shortcuts (cgroup accounting, load average, eBPF, taskstats) are not available.
- A process's `utime` and `stime` are its own CPU time since it started; its `cutime` and `cstime` are the CPU time of every child it has reaped, including those children's own reaped children.
  An orphan is reaped by pid 1 (tini), whose counters then carry it.
- The memory guard package (`oom_priority`) is stdlib-only and already runs an always-on supervisord program, `publish_cgroup_meminfo`, that reads the container's cgroup four times a second.
  It owns the agent-pid registry (`registry.live_pids_by_agent_id`), the app registry reader (`app_registry`), the shed ledger, and the `/proc`-walking helpers.
- The backup service (`host_backup`) writes its events to `$MNGR_AGENT_STATE_DIR/events/backup/events.jsonl` and reads them back with `scan_recent_ticks_across_rotation`; every supervised program inherits `MNGR_AGENT_STATE_DIR`.
- Walking directories is several times slower under gVisor than on a plain kernel (AGENTS.md), and the workspace's disk holds package caches, uploads and app data that can run to gigabytes.
- The shell restores a window at the path and query the page last reported through the app contract's location beacon; the hash is not reported.

## 2. Decisions

1. The app's display name becomes "System Monitor".
   Its package, program, port (8040), manifest name (`system-monitor`), band and launcher rank do not change, and its icon (a gauge) stays.
2. CPU is measured from per-process counters, the only accounting that exists under gVisor, by one `/proc/<pid>/stat` read per process per sample.
   The busy time of an interval is the change in the sum over every live process of `utime + stime + cutime + cstime`.
   A process that exited between two samples drops out of the sum while its parent's child counters grow by its whole lifetime, so the two cancel and nothing is counted twice or lost (section 3.2).
3. The sampler lives in the memory guard package as a second always-on program, `resource_sampler`, rather than in the app, so history outlasts the app stopping between windows.
   It samples every 5 s, keeps 10 minutes at that resolution and 24 hours of 1-minute averages, and records memory beside CPU from the same reading the guard acts on.
4. The sampler and the app share files only: the sampler writes under `data/.state/system-monitor/` (machine state the app rebuilds nothing from) and the app reads there.
   The app still writes nothing.
5. A busy stretch opens when usage stays at or above 90% of the available cores for 60 s, closes once it stays below 70% for 60 s, and is recorded only if it lasted 2 minutes.
   Attribution needs no per-process watching ahead of time: the counters are cumulative, so a baseline at the stretch's start and a reading at its end say who used the CPU in between (section 3.4).
6. The available cores are the cgroup's `cpu.max` quota where it exists (runc), else `os.cpu_count()` (gVisor reports the quota there).
   Load average is not used: it reads as zero under gVisor.
7. The overview carries no per-group disk figures, and nothing walks the disk on a timer.
   The disk view shows the disk's total, used and free from one `disk_usage` call, said to be the machine's whole disk: a local docker workspace's volume has no quota of its own, so the figure is the laptop's disk, and a cloud machine's is its data disk, shared with the container engine's own store.
   It walks the disk only when the user asks ("See what's using the disk"), reporting when it ran.
8. The page's views are real paths (`/`, `/cpu`, `/memory`, `/disk`) served by the same page, so the shell restores a window on the view it was on.
   The manifest declares no launch paths: the shell keeps synthesizing `open` at `/`, and the views are reached inside the app, so the launcher does not gain three rows for one app.
9. The app's own memory sampler thread goes away: both histories come from the sampler's files.
   When the sampler's files are missing or stale, the graphs say there are no readings yet instead of showing an empty line.
10. The backups summary reads the backup service's events and settings and never runs restic.
    Its "learn more" link opens the backup service's README in Files until a backups page exists.

## 3. Part A: the resource sampler

### 3.1 The program

`system/services/oom_priority/bin/resource_sampler.py`, run by supervisord as `resource_sampler` (`system/supervisord.conf.d/resource_sampler.conf`) under a plain `python3` with the package on `sys.path`, like `publish_cgroup_meminfo.py`.
The policy lives in `oom_priority.resource_sampler`, stdlib-only like the rest of the package; the program is the loop and the logging.
Its band is its own (`resource_sampler`), one past the app's (`system-monitor`), so it is the first built-in service shed: a shed sampler costs a gap in the graphs until supervisord restarts it, and it holds almost no memory.

Every `SAMPLE_INTERVAL_SECONDS` (5) the program takes one sample, updates its rings, rewrites the recent file, appends a minute line when a minute has closed, and steps the stretch state machine.
It waits on a `threading.Event` between samples, never `time.sleep`, and logs only when its state changes (the first sample, a stretch opening or closing, a read that starts failing or recovers).

### 3.2 One sample

- List `/proc`'s numeric entries and read each one's `stat`.
  Parse after the last `)`: field 1 is the parent pid, fields 11 to 14 are `utime`, `stime`, `cutime`, `cstime` in clock ticks.
  An entry that cannot be read (it exited mid-walk, or is not ours) is skipped.
- `ticks(pid) = utime + stime + cutime + cstime`.
  The sample keeps `{pid: (ticks, ppid)}`.
- Busy ticks since the previous sample are `sum(ticks now) - sum(ticks before)`, clamped at 0.
  A new process contributes its whole counter (it started after the previous sample); a gone process's last counter is dropped while its parent's child counters now carry it in full; a process that started and exited inside the interval is carried by its parent alone.
- Usage is `busy_ticks / (CLK_TCK * elapsed_seconds * cores)`, clamped to `[0, 1]`, with `CLK_TCK` from `os.sysconf("SC_CLK_TCK")` and `elapsed_seconds` the wall time since the previous sample.
  The first sample after start has no previous sample and records no usage.
- Per process, the same interval's share of capacity is `(ticks now - ticks before) / (CLK_TCK * elapsed_seconds * cores)`, with a missing previous reading counting as 0, less the last counter of every process gone since the previous sample, taken from its nearest ancestor still alive: that process's earlier ticks were already reported under its own pid and its whole lifetime now sits in its parent's child counters, so without the subtraction the parent would spike by the child's lifetime for one sample.
  The shares then add up to the busy ticks.
  Shares under 0.001 are left out of the file to keep it small.
- Memory is the guard's reading: the headroom file (`/mngr-vol/.host-meminfo`) while it is under 5 s old, else `/proc/meminfo`, giving total, available and used.
  This reader moves from the app's `memory.py` into `oom_priority.meminfo` and the app imports it from there.
- Cores: `cpu.max` under `/sys/fs/cgroup` read as `quota period`, giving `ceil(quota / period)`; `max` or a missing file means `os.cpu_count()`, and never fewer than 1.
  Re-read every sample, since a resize can cap a running container.

The walk reads one small file per process, about 150 of them, every 5 s.
Measured in a local `runsc` container on 2026-10-20, 150 `stat` reads plus the directory listing take about 8.5 ms (2.2 ms under runc): under 0.2% of one core at the 5 s interval, so a VM-side CPU reading for gen-2 slices was considered and dropped as not worth a second code path.

### 3.3 The files

All under `data/.state/system-monitor/`, created by the sampler.

`recent.json`, replaced by rename after every sample:

```json
{
  "version": 1,
  "sampled_at": 1760976000.0,
  "interval_seconds": 5,
  "cores": 4,
  "cpu": {"usage": 0.37, "by_pid": {"4121": 0.236, "4188": 0.059}},
  "memory": {"total": 8589934592, "available": 3113851904, "used": 5476082688},
  "samples": [[1760975400000, 0.31, 0.62], [1760975405000, 0.35, 0.62]]
}
```

`samples` holds the last 10 minutes as `[epoch_ms, cpu_usage, memory_used_fraction]`, oldest first.
On start the sampler reads this file back and keeps its samples when `sampled_at` is under 10 minutes old, so a restart does not cut the graph; a stale or unreadable file is ignored.

`history.jsonl`, appended once per closed minute:

```json
{"t": 1760975460000, "cpu": 0.33, "cpu_max": 0.71, "memory": 0.62}
```

`t` is the end of the minute in epoch milliseconds; `cpu` and `memory` are the minute's mean, `cpu_max` its highest sample.
A minute with no samples (the machine was paused, the sampler was down) gets no line, and a reader leaves a gap wider than 3 minutes blank.
When the file holds more than 2 days of lines the sampler rewrites it keeping the last day, by rename.

`events/cpu_stretches/events.jsonl`, one line per recorded stretch, in the standard event envelope:

```json
{
  "timestamp": "2026-10-20T15:12:04.000000000Z",
  "type": "cpu_stretch",
  "event_id": "cpu-stretch-6f1c…",
  "source": "cpu_stretches",
  "started_at": "2026-10-20T15:03:40.000000000Z",
  "ended_at": "2026-10-20T15:12:04.000000000Z",
  "duration_seconds": 504,
  "peak": 0.98,
  "average": 0.93,
  "cores": 4,
  "top": [
    {"kind": "chat", "agent_id": "agent-3f…", "share": 0.71},
    {"kind": "program", "program": "recipe-box", "share": 0.12}
  ],
  "other_share": 0.17
}
```

`share` is the fraction of the stretch's busy ticks.
A chat is named by its agent id (the app turns it into the chat's display name); a supervised program by its program name; anything the roots cannot claim is `other_share`.
`top` holds at most three entries, largest first, leaving out shares under 0.05.

### 3.4 Busy stretches

A state machine driven by each sample's usage:

- **Calm.** Count consecutive samples at or above `STRETCH_OPEN_USAGE` (0.90).
  At `STRETCH_OPEN_SECONDS` (60) of them the stretch opens at the time of the first such sample, and the sampler takes a baseline: the current `{pid: (ticks, ppid)}`.
- **Busy.** Every `ATTRIBUTION_INTERVAL_SECONDS` (30) the sampler folds the CPU used since the last fold into per-root accumulators, and re-baselines.
  A root is a registered agent's main pid (`registry.live_pids_by_agent_id`) or a supervised program's pid (supervisord's `getAllProcessInfo`, asked over its socket like the app does); every other live pid rolls up to its nearest ancestor root through the ppids of the current sample, or to "other" when none is.
  A pid that disappeared since the last fold had its last counter folded already and its remaining time now sits in its parent's child counters, so the fold subtracts that last counter from the root it was rolling up to; the delta of a new pid is its whole counter.
  Folding every 30 s keeps the share of a root that exits mid-stretch (a chat that stops), which a single baseline-to-end reading would hand to "other".
  Count consecutive samples below `STRETCH_CLOSE_USAGE` (0.70); at `STRETCH_CLOSE_SECONDS` (60) of them the stretch closes at the time of the first such sample.
- **Closing.** A final fold, then: a stretch shorter than `STRETCH_MIN_SECONDS` (120) is dropped; otherwise its event is appended.
  The peak and average are over the stretch's samples.

A stretch still open when the sampler stops is lost; the next start begins calm.

### 3.5 Cost

Calm: one `stat` read per process every 5 s, one `meminfo` read, one file rename.
Busy: the same, plus a registry listing and one supervisord call every 30 s.
Memory: the 10-minute ring (120 samples) and the per-pid dictionaries, under 100 KB.

## 4. Part B: the app

### 4.1 Pages and routes

| Route | Answers |
|---|---|
| `GET /`, `/cpu`, `/memory`, `/disk` | the page; its script picks the view from `location.pathname` |
| `GET /api/stats` | the overview's reading (section 4.2), shared by every window, taken at most every 4 s |
| `GET /api/history?span=1h\|24h` | both histories over the span from `history.jsonl`: 1-minute points for an hour, 5-minute means for a day |
| `GET /api/disk` | the disk's figures, the backups summary, and the last on-demand breakdown with its state |
| `POST /api/disk/scan` | starts a breakdown unless one is running; answers the state |
| `POST /api/stop` | stops what one Stop button names (section 4.6): a chat's agent, a chat's code checkers, or one of the user's apps; 400 for a body that names nothing, else the refusal's status and reason |
| `GET /_static/<basename>`, `/health`, `/api/schema` | as today |

Moving between views is a real navigation (`location.href`), so the shell's handshake reports the new path and a reopened window lands on the same view.

### 4.2 The overview reading

`StatsResponse` keeps its fields and gains:

- `cpu`: `usage` (0 to 1), `cores`, `processes` (how many the snapshot's own walk of `/proc` found), and `sampled_at` (epoch seconds of the sampler's reading, or null when the recent file is missing or older than 30 s).
- `history`: the last 10 minutes of both resources from `recent.json`'s samples, as `{t, cpu, memory}`; empty when there is no fresh file.
  The old `history` of memory alone goes away with the in-app sampler.
- Every `UsageRow` and `UsageItem` gains `cpu`: the group's share of capacity (0 to 1), or null for rows that have no processes (Shared, Temporary files).
  The System row's share is what the other rows together fall short of the whole (the sampler leaves shares under a tenth of a percent out of its file), so the column adds up to the tile; without a reading it is null like the rest.
  It is the sum over the group's processes of the sampler's `by_pid` shares, joined by pid with the snapshot's own process walk and rolled up by the same owner walk memory uses.
  A reaped child's time sits in its parent's counters, so it lands with the parent's group without any extra work.
- The Workspace row lists the workspace's own programs as items: each supervised program that is not one of the user's apps, named by its registry row (an app) or by a short table in `programs.PROGRAM_NAMES` (a service: "Memory guard", "Backups", "Agent manager", "Web access"); what no program started is "SSH access" (sshd and its sessions), "Internal system" (tini, the bootstrap and its uv wrapper, supervisord, the services agent's tmux and shell) or "Other", by its command (`usage.UNOWNED_ITEMS`).
- Where the kernel reports `smaps_rollup`, a group's memory is its programs' anonymous memory (what they made for themselves, which stopping them frees; the file pages they map are cache the kernel drops under pressure and the guard's own figure of what is in use leaves out), the Shared row is the shared memory segments programs map (the sum of `Pss_Shmem`), and the System row is the rest, so the rows add up to the memory in use.
  Under gVisor (resident sizes shrunk by one factor) a System row holds whatever the programs do not account for, so both modes have the same rows.
- Every `UsageItem` carries `stop`, the target of its Stop button (section 4.6): the chat's id, the agent id whose code checkers, or the app's program; null for the workspace's own programs, which cannot be stopped from here.
- `events`: stops (as today's `stops`, kept for the memory view) and the stretches of the last 24 hours from the sampler's events file, each with `at`, `kind` (`stop` or `busy`), `what` in plain words, and for a stretch its `duration_seconds` and `top` with agent ids resolved to chat names.

The approximate-usage mode under gVisor (`usage_mode`) is unchanged and applies to memory only; CPU shares are exact where the sampler's file is fresh.

### 4.3 The views

Each view is drawn from the readings above.

- **Overview.** Three tiles: CPU as `N% of C cores`, memory as `N% of T`, disk as `N% of T`; the CPU and memory tiles carry 10-minute sparklines, the disk tile a bar.
  Each tile opens its view.
  Then "What's using the workspace": the breakdown rows with CPU and memory columns (no disk column).
  Then "Recent events": stops and stretches of the last 24 hours, newest first, each linking to its resource's view.
- **CPU.** The headline, the usage bar, the process count; a chart with 10 min, 1 h and 24 h spans (10 min from the overview reading, longer from `/api/history`); "What's using the CPU", the groups sorted by share with the note that a chat's share includes what it runs; "Busy stretches" from the events.
  No per-core graphs (the sampler does not read them) and no load average (it does not exist under gVisor).
- **Memory.** Everything the shipped page shows, with the 90% guard mark on the bar and the chart and the span switch: the headline, the warning note from 80% with its advice, the breakdown with the temporary-files tooltip, and the stops with their Details toggles.
- **Disk.** The headline and bar from `disk_usage`, worded as the machine's whole disk (decision 7); then "What's using the disk" with a "See what's using the disk" button that posts to `/api/disk/scan`, polls `/api/disk` while scanning, and then shows the rows with "as of" the scan's time and a button to run it again; then the Backups section.
  A row that lists nothing and holds nothing (Code checkers, Your apps) is left out of every breakdown until something runs under it.

Under 480 px the tiles stack, each a row with the number on the left and the sparkline on the right, and the breakdown hides its grey hints.
The header carries no "updated N s ago": as today, it speaks only when a refresh fails.

### 4.4 The disk breakdown

Run in a thread of the app when asked, never on a timer and never twice at once.
Each bucket is one `du -sk` (GNU, the container is Linux), bounded at 120 s:

| Row | What is measured | What it lists |
|---|---|---|
| Workspace files | the top-level entries of `/home/user/workspace` in one `du`, less app data and uploads (`du --exclude`) | its folders, largest first, up to eight, with a hint for the ones a user would not know (`.venv`, `.git`, `system`) |
| Uploads | `data/uploads` | nothing |
| App data | `data/.apps` | one item per app directory |
| Package caches | `~/.cache`, `~/.npm`, `~/.local/share/uv`, `~/.local/share/pnpm`, those that exist | each cache |
| Everything else | the disk's used bytes less the rows above, never below 0 | the machine's system software (`/` less the home tree, the kernel and memory filesystems, and the container engines' stores) and whatever else is on the disk |

The result is kept in memory with its time; the app stopping forgets it, which is fine for a figure the user asked for once.
A `du` that fails or times out leaves its row out and the response says which rows are missing.
A walk that cannot read the disk at all ends in a `failed` state with no rows, so the button offers to try again rather than staying disabled.

### 4.5 Backups

`backups.py` reads the backup service's events (`$MNGR_AGENT_STATE_DIR/events/backup/events.jsonl`, through the service's own `scan_recent_ticks_across_rotation`) and its settings (`data/system/backup.toml`, through the service's loader, for the interval), and answers one of:

- not configured: no `data/.secrets/restic.env`, or the newest tick was skipped for missing secrets;
- running: a tick is in flight (with the last success's time, if any);
- up to date: the newest terminal event is a success and it is younger than twice the interval;
- stale: the newest success is older than that (with its time), or there is none yet;
- failing: the newest terminal event is a failure (with its time and the last success's).

The summary also counts failed ticks in the last 24 hours.
The link opens `system/services/host_backup/README.md` in Files through the app contract's file links.

### 4.6 Stop actions

Every item a chat, a code checker or one of the user's apps owns ends in a Stop button (an "×", shown while the pointer is over the row, and always where there is no pointer to hover with). Clicking it turns the cell into a question ("Stop this chat? Its next message starts it again." / "Quit this app? Opening it starts it again.") with the action and Cancel; the action posts `{kind, id}` to `POST /api/stop` and the next refresh shows the row gone, or the refusal in the cell until the row is next rebuilt. The route (`actions`) does one of:

- `chat`: `POST /api/chats/<id>/stop` on the chat app (its URL from the app registry), the reversible `mngr stop` the chat's own Stop runs; the chat app's refusal (converging, unknown) is relayed with its status.
- `checkers`: `SIGTERM` to the code checkers the chat's agent started, found by a fresh walk of `/proc` through the same owner walk as the breakdown; 404 when it has none.
- `app`: supervisord's `stopProcess` over its socket (`supervisor_rpc.stop_program`), only for a program the registry lists in the user band; anything else is 403. The shell starts the app again when a window opens it.

The workspace's own programs carry no target: nothing on the page can stop the chat app, the desktop, or a service.

### 4.7 Startup and failure

- The app no longer starts a sampler thread.
  Its `/api/stats` reads `recent.json` on every snapshot; a missing or stale file gives `cpu.sampled_at = null`, empty histories, null CPU shares, and the page shows "No readings yet" in the graphs and dashes in the CPU column.
- A stretch event the app cannot parse is skipped, like a bad ledger line: the app logs nothing and reports through what the page shows.
- The backups summary never raises: an unreadable events file reads as "not configured".

## 5. Part C: wiring

- `system/supervisord.conf.d/resource_sampler.conf`: `python3 system/services/oom_priority/bin/resource_sampler.py`, `autostart = true`, `autorestart = true`, through `oom_tag_service.py resource_sampler` so it takes its band.
- `bands.SERVICE_BANDS` gains `resource_sampler` one past the `system-monitor` band's value, last in the built-in order.
- The manifest: `display_name = "System Monitor"`, the description reworded; everything else as today.
  The contract tables (`docs/system/blueprint/desktop-interface/contracts.md`, `docs/system/blueprint/workspace-app-model/contracts.md`) and `system/test_app_manifests.py`'s comments follow the name.
- The app's `pyproject.toml` gains `host-backup` (for the events reader and the settings loader); `oom-priority` is already there.
- READMEs: the app's, the package's, `system/apps/README.md`, `docs/system/workspace-internals.md`, the root README, and the build-app skill's port note.

## 6. Tests

- `oom_priority/resource_sampler_test.py`: the stat parser; the busy-ticks sum over a fake `/proc` with a new, a gone and a reaped process; usage and per-pid shares with a fake clock and tick rate; cores from `cpu.max`, `max`, and no file; the recent file written and read back; the minute lines and the rewrite past two days; the stretch state machine (open after 60 s, not after 55 s, close with hysteresis, the 2-minute minimum, attribution with a root that exits mid-stretch).
- `oom_priority/bin/resource_sampler_test.py`: one `--once` round under a bare `python3` against a fake `/proc`, writing the recent file.
- `oom_priority/meminfo_test.py`: the reader's cases, moved from the app's `memory_test.py`.
- The app: `cpu_test.py` (recent file, history spans and downsampling, stretch events into plain words), `disk_test.py` (the buckets over a temporary tree, a missing bucket, the scan state), `backups_test.py` (each state from a written events file), `usage_test.py` (CPU shares rolled up by group, the System row's remainder, the stop targets, a chat's checkers by pid), `actions_test.py` (a stand-in chat app, a real process to signal, a fake supervisord socket), and `runner_test.py` for the routes and the API document.
- `system/test_app_manifests.py` and `test_supervisord_layout.py` as they are, with the new program.

## 7. Later

- A backups page the summary links to.
