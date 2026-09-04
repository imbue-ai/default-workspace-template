# browser

A per-workspace fleet of live Chromium browsers with a single atomic ownership
model: each browser is controlled by exactly one party at a time (a specific
agent, identified by its `MNGR_AGENT_ID`, or the human).

- **Daemon** (`browser-service`): a Flask + flask-sock service (synchronous,
  thread-per-connection) that owns every browser. browser_use, Playwright (async),
  and the per-browser ownership state machine run on one background asyncio event
  loop, reached from the Flask threads through a single `run_coroutine_threadsafe`
  bridge. Each browser is a **headful** Chromium (under an Xvfb virtual display, so
  it has a real X11 clipboard for native copy/paste -- see `_HEADLESS` in
  `session.py`) driven by `browser_use.BrowserSession`. Its Xvfb display is captured
  and streamed to the viewer as live H.264 (pixelflux, damage-driven stripes) plus
  Opus audio (pcmflux) over a WebSocket, with XTEST input, resize, and clipboard back
  the other way (see `videopipe.py` / `audiopipe.py` / `mediastream.py`). Each browser
  is addressed by NAME: daemon-minted ones are numbered `browser-<N>` (shown as
  "Browser N" in the workspace UI -- the same display-name/canonical-name pairing
  chats and minds hosts use), while browsers created by older builds keep their
  random english names. Closing a browser retires its name and deletes its
  profile, which is what frees the number for a later create; the fleet starts
  empty and there is no default browser.
- **Ownership** is one locked, compare-and-set state machine per browser. Agents
  never preempt each other -- a second agent waits in a FIFO queue
  (monitor-and-wait). The human can take control from the UI at any time, which
  always wins and pins the browser to the human. For direct control ownership is a
  sticky lease (acquired on the first command, re-checked before every command, and
  auto-released when idle); for `task` it is bound to the live request connection.
- **Instances** (`instances.py`, `bridged_fleet.py`): the daemon also serves the
  instances API of the workspace app model (`/_instances`, mounted on the same
  Flask app because the daemon serves its own origin; the manifest names no
  separate `instances_url`). One instance per browser: key = the name, URL
  `/?session=<name>`, title `Browser N` for a numbered name and a legacy name
  verbatim, status `working` while an agent holds control, `idle` otherwise (a
  browser still launching included), `error` once it crashed; `explicit`
  lifetime, not renameable. `new` creates through the same path as `POST
  /browsers` (409 with the reason while the fleet is full or Chromium is not
  installed), delete is the same close as `DELETE /browsers/<name>`, and a
  location report with an absolute `http(s)` URL navigates the live browser's
  active tab (409 while an agent holds it or while it is launching or crashed;
  a rooted path is 400) and checkpoints the manifest so a restart restores the
  new page. Reads, delete, and location answer 503 until the restore finishes,
  like the daemon's own state-changing routes; create does not wait, like `POST
  /browsers`. A failure of the daemon itself underneath a verb (its loop not
  answering in time, a startup error) is a 500 with a detail body. Every fleet event that changes the list or a status (a
  registration, a launch reaching `running`, a close, a crash, every ownership
  write) nudges the shell (`POST <shell>/api/apps/browser/changed`) from a
  daemon thread, so a slow shell never stalls the event loop. The existing
  `/browsers` routes stay for the CLI and the shell's passthroughs.
- **CLI** (`agentic-browser-fleet`): the thin client the agent uses to drive the
  fleet. The fleet starts empty, so the first step is always `new` (it prints the
  name of the browser it started); every other command takes that
  `<name>`. Primary path is **direct control** -- `state <name>` shows the page as
  a numbered list of clickable elements, then `open`/`click`/`input`/`scroll`/
  `keys`/`screenshot`/`tab` act on it (lifting browser-use's own executor against
  the live session). The agent does its own reasoning, so no API key is needed.
  `ls [--include-tabs]`, `new`, `acquire`/`release` round it out. An optional
  `task <name> "<goal>"` delegates a whole goal to an autonomous browser-use agent
  (the one path that needs a key). See the `agentic-browser-fleet` skill.
- **Viewer** (`assets/index.html`): a viewer-only page (no in-tab chat). It shows
  the live browser and, when an agent is driving, a grey "Agent has control"
  overlay with a "Take control" button; the agent's trace lives in the agent's
  output, not the tab.
- **Persistence**: the fleet survives a workspace stop/restart. Each browser gets
  its own persistent Chromium profile under `$MNGR_HOST_DIR/browser-profiles/`
  (Tier A -- on the workspace volume), so cookies/logins/history come back; Chromium
  does this itself, we just point `user_data_dir` at a durable dir. A tiny manifest
  (`data/.apps/browser/instances.json`, the app's instance records; a workspace from
  before the move still has it at `data/.state/browser-fleet.json`, which the daemon
  reads until its first write to the new path) records which browsers existed and
  their tab URLs. Both the profiles and the manifest live on the workspace volume and are
  captured by the restic host backup (`data/` is gitignored, so neither rides GitHub
  sync); a backup restore brings the tab list back (logged out only if the profiles
  themselves were lost). On daemon startup the
  fleet is restored **eager-sequentially** (one browser at a time, no cold-boot
  memory spike) behind an **init gate**: state-changing commands return a 503
  "initializing" until restore finishes, while `ls`/`state` stay open. A fresh
  workspace starts with an empty fleet (no default browser); the first `new`
  creates one. `close <name>` retires a browser and forgets its profile; a
  crashed browser is never restored as healthy.
  - The profile dir name contains the literal `browser-use-user-data-dir-` substring
    on purpose -- it makes browser_use's `_copy_profile()` use the dir in place
    instead of copying it to a temp dir (which would silently defeat persistence).
    Pinned by `browser-use==0.13.1` and guarded by an integration test.
- **Memory shedding** (`oom_retag.py`): Chromium's *renderers* are the most
  expendable processes in the workspace -- they hold nearly all of a browser's
  memory and shedding one costs a single tab. The daemon itself is not: it holds
  little memory, Chromium outlives its death, and supervisord restarts it into
  the same session, so it is tagged as an ordinary (most-expendable) service.
  Chromium overwrites the inherited `oom_score_adj` with its own values, which
  would leave renderers more protected than the agents they serve, so every
  fleet event that can spawn a Chromium process (launch, new page -- from any
  origin, including a human in the viewer -- and navigation) triggers a short
  burst of sweeps on a daemon thread that remaps those values across the browser
  band, renderers at the ceiling. See "The Chromium exception" in
  `system/services/oom_priority/README.md`.
