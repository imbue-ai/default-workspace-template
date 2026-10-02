# System Monitor: what is using the workspace

Status: implemented (2026-10-02). It lands as a stack of PRs, one feature each, and this document grows with them.
Audience: reviewers of the app (`system/apps/activity`), and anyone extending it or the memory machinery it reads (`system/services/oom_priority`, earlyoom, the shell's app lifecycle).

System Monitor is a built-in desktop app (package `activity`, port 8040) that answers the question a user actually has when the workspace feels slow or something vanished: *is my workspace running out of room, what is using it, and what can I do about it?* It answers in the user's terms first (chats, apps, background services) and keeps every raw number, and where it came from, one click away.

## 1. Why

Workspaces run under a hard memory cap (about 6.5 GB on the default cloud machine). Under pressure, earlyoom closes things: an agent's test run, a browser tab, sometimes a chat's agent. Before this app, a user found out only when something disappeared. Nothing on the desktop showed what was using memory, what would be closed first, or what had been closed, and an agent asked "why is my workspace slow?" had to reconstruct it from `/proc` by hand.

Goals:

- **Plain first, technical on demand.** A headline anyone can read; below it, things the user recognizes; inside each, the processes, command lines, shedding priorities and the file or command each figure came from.
- **Honest.** Every figure names its source. A source that cannot be read becomes a note on the page, never an empty list or a zero. Predictions are labelled as predictions.
- **No idle cost.** The shell stops the app a minute after its last window closes (`stop_when_no_windows`), and nothing it adds stays resident.
- **Safe actions only.** The user can stop what is safe to stop (a chat), told first what happens. Nothing is killed raw.

Non-goals for this change: alerts while the app is closed, stopping arbitrary apps (a stacked change), and acting on the user's behalf.

## 2. What the user sees

**Memory tab**

1. A headline against the workspace's real limit, in three states: room to spare, getting tight, about to start closing things. The status is measured against *where closing starts* (section 4.1), not against the raw limit.
2. A bar of where memory goes: chats, apps and background services, with a mark where closing starts. Each section's colour carries through to its heading and its rows' bars, and each legend entry jumps to its section.
3. Memory over time for the last hour, 24 hours or 7 days, with the closing line, a mark wherever something was closed, a hover readout and a table view. Below it, "Recently closed" lists the 20 newest closures of the last week: what was closed, how much it freed, and what to do about it ("Its conversation is kept; send it a message to carry on").
4. Questions a newcomer would ask (what happens if memory fills up, how do I free it, can I get more), answered in place and picked for what the page shows. "Ask in chat" drafts the question, with the page's numbers, into the user's current chat without sending it.
5. When memory is getting tight, "Ways to free up memory" lists chats idle for 15 minutes or more, largest first, each with Stop.
6. Every chat (all harnesses), app and background service, with its size and share of memory in use ("171 MB · 7%"). Every running chat offers Stop (a working one warns that stopping interrupts it) and a stopped chat offers Start; the process most likely closed first is flagged. Each row opens to its processes.

## 3. Architecture

```
  desktop window (Mithril page, app origin)            cron, once a minute
        |  GET /api/summary   every 5 s, while shown        |
        |  GET /api/history   every 60 s, while shown       v
        |                                              activity-record-memory
        |  POST /api/chats/<id>/stop|start                  |  one reading, appended
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
        +-- oom_priority shed ledger (what earlyoom closed)
```

The backend reads everything per request and holds no state between requests. The frontend polls only between the shell's `shell:shown` and `shell:hidden`, and while the browser tab is visible. The manifest sets `stop_when_no_windows`, so the shell stops the app a minute after its last window closes and starts it again on the next request (`docs/system/specs/stop-when-no-windows.md`; a minimized window, a share grant or a request from an agent keeps it up a while longer).

Code map (`system/apps/activity/src/activity/`):

| Module | Role |
|---|---|
| `memory_reading.py` | The memory reading and where closing starts (section 4.1) |
| `processes.py` | The process table from `/proc`, gVisor-aware |
| `summary.py` | Crediting processes to chats, apps and services; the headline; the likely first to close (pure) |
| `readings.py` | Taking each reading independently; a failed one becomes a note |
| `chats.py` | The chat app's list and its stop/start, with lenient wire models |
| `supervised_programs.py` | supervisord's `getAllProcessInfo` over its unix socket |
| `history.py`, `record_memory.py`, `cron_entry.py`, `history_view.py` | Memory over time (section 4.5) |
| `closures.py` | Shed-ledger records in plain words |
| `request_guard.py`, `pages.py` | The owner-only API, the write guard, the routes |
| `main.py`, `serving.py` | Registration, the recorder's install, and the background server |

The frontend (`frontend/src/`) is Mithril, TypeScript and Tailwind on the shared `workspace_ui` library: `models/` (fetching, one shared poller), `views/` (the memory tab, the chart, formatting and copy), with pure helpers (`chartGeometry.ts`, `format.ts`, `questions.ts`) kept apart from components so they test without a DOM.

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

The app cannot watch memory itself without costing memory, so a cron job does: `activity-record-memory` runs once a minute, takes the same reading the headline uses, appends one line (`<epoch seconds>\t<used KiB>\t<limit KiB>`) to `data/.state/activity/memory-history.tsv` and exits. Measured in a workspace-image container it takes about 70 ms and peaks at 33 MB, then holds nothing.

Once the file passes 512 KiB the recorder rewrites it without readings older than a week, atomically (temporary file, then `os.replace`). Only the recorder writes the file; readers skip malformed or half-written lines, and undecodable bytes cost only their own line.

The entry is code-owned, so like the bootstrap's `update-apply-recover` guard it lives only in `/etc/cron.d/activity-memory-history`, never in the user-editable `data/.state/cron.d/`, where it would read as the user's own schedule (and come back after they removed it). `/etc/cron.d` is on the container rootfs; the app rewrites the entry each time it starts, which it does at every boot (its program autostarts before the shell stops it). It skips `with_agent_env.sh`: the recorder needs no agent credentials, and history should keep recording when the agent environment is broken. `manage-scheduled-tasks` documents it as the second such built-in entry.

`GET /api/history?range=hour|day|week` groups readings into periods (1, 5 or 30 minutes) with each period's average, lowest and highest, so a short spike survives grouping. A period with no reading is left out, so a gap in recording draws as a gap, not a line across it. The same document carries the closures in range and the last week's, read from earlyoom's shed ledger (`oom_priority.paths.shed_ledger_path()`) and described in plain words: a chat's agent, a browser tab, or a program an agent was running, each with what to do next.

### 4.6 Asking in chat

The page's answers cover the common questions. When they are not enough, "Ask in chat" drafts the user's question, followed by "I'm looking at System Monitor" and the figures on screen, into the user's current chat through the shell contract's `draftText`, unsent, so it never starts a new chat. The app's README tells the answering agent to read the live figures (`curl localhost:8040/api/summary`, `/api/history`) rather than trust the quoted ones, and to follow `.agents/shared/references/freeing-memory.md`: list candidates, let the user choose, and stop nothing on their behalf.

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
- **No raw kill.** The only action is the chat app's own stop.

## 7. Failure handling

Each reading is taken independently (`readings.py`). If the chat app does not answer, chats are listed by their agents' own names (with no Stop, since a chat id is unknown) and the page says so. If supervisord does not answer, apps read "state unknown", background services drop out of the list, and their memory is counted as plumbing, again with a note. Every outbound call has a hard timeout (chat list 5 s, supervisord 2 s, chat stop or start 60 s), so a hung dependency slows a refresh to its timeout rather than hanging the page. An unreadable registry, ledger or history file degrades to a note or an empty chart that explains itself ("Nothing recorded yet", "Recording has paused"), never to a failed page. Parsing of every other app's output is lenient (`extra="ignore"`), so a newer chat app or registry does not break an older System Monitor. The page keeps showing the last good figures when a refresh fails, and says since when they are stale.

## 8. Wiring into the template

`system/supervisord.conf.d/activity.conf` (oom band `activity`, shed after Getting Started), the image build (`system/Dockerfile`), the npm workspace, `uv.lock`, `system/test_app_manifests.py`, update-self's frontend bundle list (`update_layout.py`; its list of rebuild-triggering directories is now derived from the bundle list, so a new frontend cannot be missed), and the docs tables (`desktop-interface/contracts.md`, `workspace-app-model/contracts.md`, `stop-when-no-windows.md`). `freeing-memory.md` points agents at the app.

## 9. Testing

- **Backend** (`cd system/apps/activity && uv run pytest`): 107 tests, 96% line coverage, covering each memory source and the closing point under cloud, runc and gVisor layouts; Linux and gVisor `/proc` (including non-UTF-8 `smaps`); crediting and the first-to-close prediction; the chat stop re-check; each source failing on its own (the chat app, supervisord, the registry, memory) still answering with a note and every process drawn; previews refusing writes; the owner and write guards (with a regression test for the dropped `Host`); history append, rotation, grouping and corrupt bytes; closure descriptions, including malformed ledger records; the cron entry; and the supervisord reader against a real unix-socket XML-RPC server. The app's ratchets (`test_activity_ratchets.py`) pass, and `ty` and `ruff` are clean.
- **Frontend** (`cd system && npm test --workspace=apps/activity/frontend`): 62 vitest tests: the poller's stop-and-start-while-a-read-is-in-flight behaviour, stale range replies, the forbidden state, chat-action results, the stop dialog's re-ask flow, the memory bar's scaling, chart geometry and rendering (gaps, closures, empty and paused states), copy, and WCAG AA contrast of every colour pair against `workspace_ui`'s tokens, with a scan that fails if a view uses a text colour no pair checks.
- **Template suites**, run as CI runs them in a workspace-image container on the whole stack: the root suite (3370 passed; 2 tests fail identically on unmodified `main`), and `system_interface` (687) and `chat` (2026) with `-m ''`. On each layer of the stack, the app's suites, every frontend check and the always-run checks pass.
- **Manual**: the app against seeded history and a seeded shed ledger, checked at desktop and phone widths; the recorder run by hand; the memory tab, ask-in-chat and stopping a chat in a local Studio workspace.

## 10. Limitations

- Closures the kernel makes at the container's own limit are recorded nowhere, so they cannot be listed; the page says so when the kernel is the closer.
- The recorder samples once a minute: a spike shorter than that can fall between two readings (the chart's "How this is recorded" says so).
- On a local workspace, the closing point assumes nothing outside the workspace is using the machine's memory (section 4.1).
- There is a small window between the stop re-check and the stop itself in which a chat could start a turn; the chat app's stop is still safe, but the user is not warned in that window.
- A request with no identity header counts as the owner (section 6). A page in the agents' browser inside the container that reached this port by DNS rebinding would pass both guards; the shell has the same exposure, and whether Chromium's Private Network Access blocks it is unverified.
- `serving.py`, the supervisord socket client and the identity parsing are copies of code in other apps (Getting Started, the shell); see section 11.

## 11. Next steps

- **Stop apps** (stacked change): stop a non-critical app through the shell's own Quit, never the browser or this app.
- **Shared libraries**: move the background server, the supervisord socket client and the identity parsing into shared libraries (`app_manifest` or a new one), so the apps that copy them cannot drift.
- **Design-only ideas**: agent hooks that read the history before starting a memory-heavy command; automatic stopping of long-idle chats. The latter conflicts with "memory is the user's to spend" (`freeing-memory.md`) and needs the user's opt-in, so it stays a proposal.
