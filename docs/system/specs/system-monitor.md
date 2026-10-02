# System Monitor: what is using the workspace

Status: implemented (2026-10-02). It lands as a stack of PRs, one feature each, and this document grows with them.
Audience: reviewers of the app (`system/apps/activity`), and anyone extending it or the memory machinery it reads (`system/services/oom_priority`, earlyoom, the shell's app lifecycle).

System Monitor is a built-in desktop app (package `activity`, port 8040) that answers the question a user actually has when the workspace feels slow or something vanished: *is my workspace running out of room, what is using it, and what can I do about it?* It answers in the user's terms first (chats, apps, background services, your files) and keeps every raw number, and where it came from, one click away.

## 1. Why

Workspaces run under a hard memory cap (about 6.5 GB on the default cloud machine). Under pressure, earlyoom closes things: an agent's test run, a browser tab, sometimes a chat's agent. Before this app, a user found out only when something disappeared. Nothing on the desktop showed what was using memory, what would be closed first, or what had been closed, and an agent asked "why is my workspace slow?" had to reconstruct it from `/proc` by hand.

Goals:

- **Plain first, technical on demand.** A headline anyone can read; below it, things the user recognizes; inside each, the processes, command lines, shedding priorities and the file or command each figure came from.
- **Honest.** Every figure names its source. A source that cannot be read becomes a note on the page, never an empty list or a zero. Predictions are labelled as predictions.
- **No idle cost.** The shell stops the app a minute after its last window closes (`stop_when_no_windows`), and nothing it adds stays resident.
- **Safe actions only.** The user can stop what is safe to stop (a chat, a non-critical app), told first what happens. Nothing is killed raw.

Non-goals for this change: notifications while the app is closed (section 4.9 says why), a disk quota figure (not readable from inside the container, see section 4.6), and acting on the user's behalf.

## 2. What the user sees

**Memory tab**

1. A headline against the workspace's real limit, in three states: room to spare, getting tight, about to start closing things. The status is measured against *where closing starts* (section 4.1), not against the raw limit.
2. A bar of where memory goes: chats, apps and background services, with a mark where closing starts. Each section's colour carries through to its heading and its rows' bars, and each legend entry jumps to its section.
3. Memory over time for the last hour, 24 hours or 7 days, with the closing line, a mark wherever something was closed, a hover readout and a table view. Below it, "Recently closed" lists the 20 newest closures of the last week: what was closed, how much it freed, and what to do about it ("Its conversation is kept; send it a message to carry on").
4. Questions a newcomer would ask (what happens if memory fills up, how do I free it, can I get more), answered in place and picked for what the page shows. "Ask in chat" drafts the question, with the page's numbers, into the user's current chat without sending it.
5. While memory has stayed tight for five minutes or more, a warning under the headline says for how long and since when (section 4.9).
6. When memory is getting tight, "Ways to free up memory" lists chats idle for 15 minutes or more, largest first, each with Stop.
7. Every chat (all harnesses), app and background service, with its size and share of memory in use ("171 MB · 7%"). Every running chat offers Stop (a working one warns that stopping interrupts it) and a stopped chat offers Start; a running app the shell may quit offers Stop, except the browser and System Monitor itself (section 4.8); the process most likely closed first is flagged. Each row opens to its processes.

**Storage tab**: what is taking up disk, grouped as your files, chats and agents, app data, installed tools, download caches and logs, with the total of the measured folders, each group's folders and the largest folders. It measures the first time the tab opens and then only when asked, never on a timer.

## 3. Architecture

```
  desktop window (Mithril page, app origin)            cron, once a minute
        |  GET /api/summary   every 5 s, while shown        |
        |  GET /api/history   every 60 s, while shown       v
        |  GET /api/storage   on open / on request     activity-record-memory
        |  POST /api/chats/<id>/stop|start                  |  one reading, appended
        |  POST /api/apps/<name>/stop                       |
        v                                                   v
  activity-app (Flask on 127.0.0.1:8040) ------------> data/.state/activity/memory-history.tsv
        |
        +-- memory: /mngr-vol/.host-meminfo | cgroup v2 | /proc/meminfo
        +-- earlyoom's own argv and the meminfo it reads (where closing starts)
        +-- /proc (every process: parent, comm, cmdline, memory, oom_score_adj)
        +-- oom_priority agent-pid registry (which processes are which agent)
        +-- supervisord XML-RPC socket (apps' and services' programs and pids)
        +-- data/.state/apps.toml (the app registry: names, criticality)
        +-- chat app GET /api/chats (titles, harness, status) and its stop/start
        +-- the shell's POST /api/apps/<name>/quit (stopping an app)
        +-- oom_priority shed ledger (what earlyoom closed)
        +-- du -sk over category folders (storage, on request)
```

The backend reads everything per request and holds no state between requests (a try-lock turns away a second `du` while one runs). The frontend polls only while the memory tab is selected, between the shell's `shell:shown` and `shell:hidden`, and while the browser tab is visible. The manifest sets `stop_when_no_windows`, so the shell stops the app a minute after its last window closes and starts it again on the next request (`docs/system/specs/stop-when-no-windows.md`; a minimized window, a share grant or a request from an agent keeps it up a while longer).

Code map (`system/apps/activity/src/activity/`):

| Module | Role |
|---|---|
| `memory_reading.py` | The memory reading and where closing starts (section 4.1) |
| `processes.py` | The process table from `/proc`, gVisor-aware |
| `summary.py` | Crediting processes to chats, apps and services; the headline; the likely first to close (pure) |
| `readings.py` | Taking each reading independently; a failed one becomes a note |
| `chats.py` | The chat app's list and its stop/start, with lenient wire models |
| `apps.py` | Which apps the page may stop, and the shell's Quit (section 4.8) |
| `supervised_programs.py` | supervisord's `getAllProcessInfo` over its unix socket |
| `history.py`, `record_memory.py`, `cron_entry.py`, `history_view.py` | Memory over time (section 4.5) |
| `closures.py` | Shed-ledger records in plain words |
| `storage.py` | Disk use by category |
| `request_guard.py`, `pages.py` | The owner-only API, the write guard, the routes |
| `main.py`, `serving.py` | Registration, the recorder's install, and the background server |

The frontend (`frontend/src/`) is Mithril, TypeScript and Tailwind on the shared `workspace_ui` library: `models/` (fetching, one shared poller), `views/` (the two tabs, the chart, formatting and copy), with pure helpers (`chartGeometry.ts`, `format.ts`, `questions.ts`) kept apart from components so they test without a DOM.

## 4. How it works

### 4.1 Memory, and where closing starts

The reading comes from the most truthful source available, and says which one it used:

1. `/mngr-vol/.host-meminfo` on a cloud workspace: the VM publishes its own meminfo into the container, and that is what earlyoom reads there.
2. The container's cgroup v2 files otherwise: `memory.max` as the limit, `memory.current - inactive_file` as used (reclaimable page cache is not pressure).
3. `/proc/meminfo` last.

"Where closing starts" is computed rather than assumed, because two different things close processes depending on the workspace. earlyoom acts once available memory falls below its `-m` threshold of the meminfo *it* reads, so the point is parsed from earlyoom's live argv (`/proc/<pid>/cmdline`) and applied to that meminfo. Under runc (a local Docker workspace), the kernel's cgroup cap can sit below earlyoom's point, in which case the kernel's OOM killer closes things first at the cap, and the page says so (`MemoryCloser.SYSTEM_LIMIT`). earlyoom also waits until free swap is below its `-s` threshold (it runs with `-m 10,5 -s 10,5`; both default to 10% when absent); on a machine with swap the page says so, since with swap to spare it can act later than the memory point alone. On a local workspace earlyoom reads the whole machine's memory, so the page states that its closing point assumes nothing outside the workspace is using it.

The headline steps are fractions of the closing point (`summary.py`: "getting tight" from 83%, "about to close" from 98%).

### 4.2 Crediting processes to things the user recognizes

Each process is credited to the nearest ancestor (or itself) that is a known root:

- an agent's registered pids (`data/.state/oom_priority/agent_pids/<pid>.json`), mapped to its chat through every agent id the chat app lists for it (a chat that switched agents keeps its earlier agent's processes), else shown as a helper agent or an unchatted agent;
- a supervisord program's pid, as the app it registers (via the app registry) or as a background service;
- Chromium processes with no other owner, to the browser app;
- anything else, to "workspace plumbing": supervisord, tmux and sshd, and any process nothing above accounts for (a server an agent started and left running, for example, once its parent exits).

Sizes are what earlyoom counts: `VmRSS` on Linux, and on gVisor (where `VmRSS` is inflated and `RssAnon` is absent) the sum of `smaps` `Anonymous:` lines. A row's share is out of memory in use, or out of the summed process sizes when shared memory (counted once in each process) makes them larger, so the bar, the sections and the rows always agree.

### 4.3 Which process is likely closed first

earlyoom in this workspace is a fork that scores `VmRSS + VmSwap + VmPTE + oom_score_adj × (MemTotal + SwapTotal) / 1000`; a process matching `--avoid` is scored 300 `oom_score_adj` points lower (less likely closed), and `oom_score_adj = -1000` is never killed. That model lives in `oom_priority/badness.py`, moved there from `oom_drill.py` so the drill and the app predict identically, and the page flags the top pick ("Likely closed first if memory runs out"). When the kernel closes first, its own ordering is used instead: the same score without `--avoid`, over the cgroup's limit. It is labelled a prediction: priorities change as things start, finish and grow.

### 4.4 Stopping and starting a chat

Chats are stopped through the chat app's own routes, never with `mngr` directly, because the chat app owns a chat's state (and refuses the primary services agent and a chat mid-handoff). A chat id reaches the chat app only for a chat its own list holds, path-quoted.

The page may be seconds old, so the backend re-reads the chat's status before stopping. If the chat started working since the page last read it, the backend answers `409` with the new status and the dialog asks again, now warning that stopping interrupts what it is doing. The confirmation states the consequence: memory freed (approximately), conversation kept, the chat picks up when messaged, and helper agents keep running.

### 4.5 Memory over time

The app cannot watch memory itself without costing memory, so a cron job does: `activity-record-memory` runs once a minute, takes the same reading the headline uses, appends one line (`<epoch seconds>\t<used KiB>\t<limit KiB>\t<source>`, the source being the host meminfo, the cgroup or `/proc/meminfo`) to `data/.state/activity/memory-history.tsv` and exits. Readers ignore columns they do not know, so the format can grow without an older reader (after a rollback, say) dropping every line. Measured in a workspace-image container it takes about 70 ms and peaks at 33 MB, then holds nothing.

Once the file passes 512 KiB the recorder rewrites it without readings older than a week, atomically (temporary file, then `os.replace`). Only the recorder writes the file; readers skip malformed or half-written lines, and undecodable bytes cost only their own line.

The entry is code-owned, so like the bootstrap's `update-apply-recover` guard it lives only in `/etc/cron.d/activity-memory-history`, never in the user-editable `data/.state/cron.d/`, where it would read as the user's own schedule (and come back after they removed it). The line runs the recorder under `flock -n` and `timeout 50`, as the bootstrap's guard serializes itself, so a stalled read never stacks up a recorder a minute, and the entry is replaced atomically, since cron rescans the directory while the app runs. `/etc/cron.d` is on the container rootfs; the app rewrites the entry each time it starts, which it does at every boot (its program autostarts before the shell stops it). It skips `with_agent_env.sh`: the recorder needs no agent credentials, and history should keep recording when the agent environment is broken. `manage-scheduled-tasks` documents it as the second such built-in entry.

`GET /api/history?range=hour|day|week` groups readings into periods (1, 5 or 30 minutes) with each period's average, lowest and highest, so a short spike survives grouping. A period with no reading is left out, so a gap in recording draws as a gap, not a line across it. The same document carries the closures in range and the last week's, read from earlyoom's shed ledger (`oom_priority.paths.shed_ledger_path()`) and described in plain words: a chat's agent, a helper agent, a browser tab, a background program, or a program an agent was running, each with what to do next.

### 4.6 Storage

One `du -sk` (60 s timeout) over disjoint category folders (a test keeps them disjoint), so nothing is counted twice, plus the largest folders. The categories cover the user's files, chats and agents (including the worktrees helper agents work in), app data, apps and tools, saved versions (the workspace's git history), download caches and logs. If `du` runs out of time, the folders it finished are kept and the page says the total is incomplete; a note like that shows on the page, not only in the details. It runs the first time the tab opens and then only when the user asks, never on a timer, and a second request while one runs is answered `429`. The total shown is the sum of the measured folders; no disk size or quota is shown: `df` inside the container reports the host's disk, and a cloud workspace's quota is set outside the container and not published inside it.

### 4.7 Asking in chat

The page's answers cover the common questions. When they are not enough, "Ask in chat" drafts the user's question, followed by "I'm looking at System Monitor" and the figures on screen, into the user's current chat through the shell contract's `draftText`, unsent, so it never starts a new chat. The app's README tells the answering agent to read the live figures (`curl localhost:8040/api/summary`, `/api/history`, `/api/storage`) rather than trust the quoted ones, and to follow `.agents/shared/references/freeing-memory.md`: list candidates, let the user choose, and stop nothing on their behalf.

### 4.8 Stopping an app

A running app's row offers Stop. After a confirmation, the backend forwards to the shell's own `POST /api/apps/<name>/quit` (`docs/system/specs/stop-when-no-windows.md`, Part E), reached at `app_manifest.shell_windows.shell_base_url()` as every app reaches the shell. Quit closes the app's windows on every desktop (pinned windows stay, and a pinned window's requests can start the app again) and stops its program; the shell then holds the app's port and starts it again on the next request, so a stopped app comes back when it is next opened. The confirmation says exactly that, including that shared desktops lose their windows too.

Which apps offer Stop is one pure rule, `apps.is_app_stoppable` (a root-level test, `system/test_stoppable_rule_agreement.py`, keeps its copy of the shell's rule agreeing with the shell's own): what the shell itself would quit (`is_quittable_by_shell`, mirroring the shell's `stoppable_program_of`: a supervised program, never a critical app or a row inside a critical app's program), less two exclusions this app makes:

- the browser, because agents drive it and stopping it would pull it from under them; its row shows an "Always on" badge and reads "The browser your agents use · Running because agents use it";
- System Monitor itself, because stopping it would close the window asking.

The backend checks the rule again before forwarding (the page's view may be stale, and a request need not come from it), and refuses an unreadable registry rather than guessing. The page also re-checks the row when the user confirms, so a refresh that already found the app stopped does not report memory freed. A row's "starts when you open it" line follows the shell's rule rather than the page's offer, so a browser stopped from its own window menu reads correctly.

### 4.9 Warnings while memory stays tight

When memory has stayed at or above "getting tight" for five readings in a row, a minute apart (five minutes, each reading standing for its minute), the memory tab shows a warning under the headline: for how long, since when, the peak, and that stopping something unused makes room. For an hour after it eases, a quieter line says it was tight earlier and until when.

The rule is one pure function, `pressure.latest_pressure`, over the recorder's history plus the live reading as the newest point, evaluated whenever the page reads the summary. There is no alert state: nothing to go stale, nothing to clear. A missed reading (a gap of more than 90 seconds) or a single reading below the line ends a stretch, so a warning always describes memory that was tight minute after minute, and the live point means the warning never outlasts the headline by the recorder's minute. Durations come from the workspace's own timestamps, not the browser's clock; an eased stretch is not mentioned while the live reading is tight again; readings stamped after the live one (a clock stepped back) are left out; and only the last two days of history are read, so the summary does not parse a week of readings every few seconds.

The warning is seen only while System Monitor is open. A notification while it is closed was investigated and left out:

- The chat notification route (`.agents/skills/notify-user/`) is for chat agents only. Called from a background job through the services agent's environment, it would appear as a message from "system-services", and clicking it would open something that is not a chat. The feed also does no deduplication.
- The desktop shell has no notice route an app or job can post to; its toasts exist only in the browser.
- No background service in the template notifies the user directly today; the memory guard's own notice goes to the agent it revived.

A proper notification needs a paired mngr change: a workspace system-event route on the minds API, a feed card whose click opens System Monitor, and server-side deduplication. The recorder would then apply the same rule each minute and post once per stretch (section 11).

## 5. Cost

| State | Memory |
|---|---|
| No window open | Nothing: the shell has stopped the app and holds its port. |
| Window open | About 61 MB RSS (49 MB proportional) for the Flask process, measured after serving the summary and history in a workspace-image container. |
| Recording | One process a minute for about 70 ms, peaking at 33 MB. |

A summary request takes about 100 ms (it walks `/proc` and asks the chat app and supervisord); a week's history about 20 ms.

## 6. Trust and safety

- **Owner only.** Every `/api/` route except the health probe requires the `X-Imbue-Identity` header's `owner` flag, parsed with the same lenient pydantic model as the shell (`{"owner": "false"}` is a visitor in both). As in the shell, a request with no header, a malformed one, or one without an `owner` key counts as the owner: no current proxy stamped it, so it came from inside the workspace, which is how agents read the figures. The page shows every chat's name and every command line and can stop chats, none of which a visitor the owner shared one app with should reach.
- **Writes only from this app's page.** A write must be JSON (a browser cannot send it cross-origin without a CORS preflight the server never approves) and must come from the app's own page: `Sec-Fetch-Site: same-origin`, or for a browser that predates that header, an `Origin` whose first label is the app's unguessable origin label. `Host` cannot be used, because the desktop's forwarder drops it. A write with no `Origin` (curl, an agent's script) is inside the trust boundary.
- **Previews.** A preview of a proposed change to this app (`activity-app --no-register`) registers nothing, installs no recorder, and reads the registry copy the preview machinery provides (`MINDS_APPS_FILE`). It shows the live workspace, so it must change nothing in it: every write answers it `403` in the shell preview's words, the summary says `is_preview`, and the page offers no Stop or Start and says why.
- **No raw kill.** The only actions are the chat app's own stop and the shell's own Quit.

## 7. Failure handling

Each reading is taken independently (`readings.py`). If the chat app does not answer, chats are listed by their agents' own names (with no Stop, since a chat id is unknown) and the page says so. If supervisord does not answer, apps read "state unknown", background services drop out of the list, and their memory is counted as plumbing, again with a note. Every outbound call has a hard timeout (chat list 5 s, supervisord 2 s, chat stop or start 60 s, the shell's Quit 30 s), so a hung dependency slows a refresh to its timeout rather than hanging the page. An unreadable registry, ledger or history file degrades to a note or an empty chart that explains itself ("Nothing recorded yet", "Recording has paused"), never to a failed page. Parsing of every other app's output is lenient (`extra="ignore"`), so a newer chat app or registry does not break an older System Monitor. The page keeps showing the last good figures when a refresh fails, and says since when they are stale.

## 8. Wiring into the template

`system/supervisord.conf.d/activity.conf` (oom band `activity`, shed after Getting Started), the image build (`system/Dockerfile`), the npm workspace, `uv.lock`, `system/test_app_manifests.py`, update-self's frontend bundle list (`update_layout.py`; its list of rebuild-triggering directories is now derived from the bundle list, so a new frontend cannot be missed), and the docs tables (`desktop-interface/contracts.md`, `workspace-app-model/contracts.md`, `stop-when-no-windows.md`). `freeing-memory.md` points agents at the app.

## 9. Testing

- **Backend** (`cd system/apps/activity && uv run pytest`): 150 tests, 97% line coverage, covering each memory source and the closing point under cloud, runc and gVisor layouts; Linux and gVisor `/proc` (including non-UTF-8 `smaps`); crediting and the first-to-close prediction; the chat stop re-check; the pressure rule (sustained stretches, spikes, dips, gaps, eased and stale stretches, the live point); which apps may be stopped, the forwarded Quit, its refusals and previews; each source failing on its own (the chat app, supervisord, the registry, memory) still answering with a note and every process drawn; previews refusing writes; the owner and write guards (with a regression test for the dropped `Host`); storage grouping and the concurrent-measure lock; history append, rotation, grouping and corrupt bytes; closure descriptions, including malformed ledger records; the cron entry; and the supervisord reader against a real unix-socket XML-RPC server. The app's ratchets (`test_activity_ratchets.py`) pass, and `ty` and `ruff` are clean.
- **Frontend** (`cd system && npm test --workspace=apps/activity/frontend`): 87 vitest tests: the pressure warning and its quieter eased form; the app stop's confirmation, refusal, keep-running and stale-row paths and its absence in previews; the poller's stop-and-start-while-a-read-is-in-flight behaviour, stale range replies, the forbidden state, chat-action results, the stop dialog's re-ask flow, the memory bar's scaling, the storage tab, chart geometry and rendering (gaps, closures, empty and paused states), copy, and WCAG AA contrast of every colour pair against `workspace_ui`'s tokens, with a scan that fails if a view uses a text colour no pair checks.
- **Template suites**, run as CI runs them in a workspace-image container on the whole stack: the root suite (3370 passed; 2 tests fail identically on unmodified `main`), and `system_interface` (687) and `chat` (2026) with `-m ''`. On each layer of the stack, the app's suites, every frontend check and the always-run checks pass.
- **Manual**: the app against seeded history and a seeded shed ledger, checked at desktop and phone widths; the recorder run by hand; the memory and storage tabs, ask-in-chat, stopping a chat and stopping an app in a local Studio workspace.

## 10. Limitations

- Closures the kernel makes at the container's own limit are recorded nowhere, so they cannot be listed; the page says so when the kernel is the closer.
- The recorder samples once a minute: a spike shorter than that can fall between two readings (the chart's "How this is recorded" says so).
- Storage shows no disk size or quota, only the total of the folders it measured (section 4.6).
- On a local workspace, the closing point assumes nothing outside the workspace is using the machine's memory (section 4.1).
- There is a small window between the stop re-check and the stop itself in which a chat could start a turn; the chat app's stop is still safe, but the user is not warned in that window.
- A request with no identity header counts as the owner (section 6). A page in the agents' browser inside the container that reached this port by DNS rebinding would pass both guards; the shell has the same exposure, and whether Chromium's Private Network Access blocks it is unverified.
- A folder whose name contains a newline breaks `du`'s one-line-per-path output and is left out of the storage totals.
- A process whose parent exited before any agent or program claimed it (a dev server an agent started and left running) is counted under workspace plumbing, not under the agent that started it.
- Errors reach the page in their technical wording: a failed refresh shows the browser's or the backend's reason, and a refused stop or start shows the chat app's detail (as a `502`, whatever status the chat app gave).
- The status lines (an action's result, stale figures, a drafted question) are created with their text, which screen readers announce unreliably.
- The package, its data folder, its oom band and its cron entry are named `activity`, while users see "System Monitor"; renaming the package after it ships means migrating the registry row, the data folder and the cron entry (section 11).
- `serving.py`, the supervisord socket client and the identity parsing are copies of code in other apps (Getting Started, the shell); see section 11.

## 11. Next steps

- **Notifications while the app is closed**: the mngr-side system-event route and feed card (section 4.9), with the recorder posting once per stretch.
- **A desktop-wide banner**: the shell could show the same warning on every desktop; it needs a shell contract addition and the critical-app flow.
- **Disk quota on cloud workspaces**: publish the VM's disk use into the container as `.host-meminfo` does for memory, so the storage tab can show used against the limit.
- **Shared libraries**: move the background server, the supervisord socket client and the identity parsing into shared libraries (`app_manifest` or a new one), so the apps that copy them cannot drift.
- **Decide the package name before the first merge**: keep `activity` with "System Monitor" as its display name, or rename the package to `system-monitor` while nothing persisted depends on it yet.
- **Plain-language errors and announcements**: a plain sentence with the technical reason behind a details disclosure, chat-app refusals passed through with their own status, and one persistent `role="status"` region whose text changes.
- **Credit orphaned processes**: attribute a process with no claimed ancestor through the `MNGR_AGENT_ID` in its environment, and show what remains as "Other processes" rather than plumbing.
- **A Host check against DNS rebinding**: refuse `/api/*` unless `Host` names this app's loopback address, once what `Host` the share gateway forwards is confirmed.
- **Smaller pieces**: split `summary.build_summary` (one long function with a many-argument item builder and string-prefixed ids) and the `ActivityPage` component into smaller units.
- **Design-only ideas**: agent hooks that read the history before starting a memory-heavy command; automatic stopping of long-idle chats. The latter conflicts with "memory is the user's to spend" (`freeing-memory.md`) and needs the user's opt-in, so it stays a proposal.
