# Stop apps that have no windows

Status: agreed design (2026-09-28), implemented on `mngr/stop-closed-apps`.
Audience: implementers of the shell (`system/apps/system_interface`), `system/libs/app_manifest`, `system/scripts/forward_port.py`, the share gateway (`system/services/share_gateway`), the built-in app manifests, the `build-app` skill, and the supervisord drop-ins.

This spec amends the desktop interface ([plan](../blueprint/desktop-interface/plan-desktop-interface.md), [contracts](../blueprint/desktop-interface/contracts.md)) and the window-bound resources spec ([window-bound-resources.md](window-bound-resources.md)) in one place: what happens to an app's *process* once nobody is looking at the app.

- **Part A: no resident `uv run`.** The root-venv services exec their entry point instead of living under a `uv run` parent.
- **Part B: port parking.** The shell holds a stopped app's port and starts the app on the first connection, so a stopped app is reachable and neither `mngr forward` nor the share gateway learns anything about starting apps.
- **Part C: `stop_when_no_windows`.** One manifest field says whether the shell may stop an app that has no windows.
- **Part D: stop when no windows.** The shell stops such an app once no window on any desktop shows it for a grace period.
- **Part E: Quit.** The window menu's Stop and Start become one Quit, which closes every window of the app and stops it at once.
- **Part F: the app watcher moves into the shell.** The shell announces registrations to the mngr services event file itself.

## 1. Background

Facts this design builds on, as of `main` at `02eb68c02` (2026-09-28).

- Every app and background service is a supervisord program with `autostart = true` and `autorestart = true`, and stays up for the life of the workspace whether or not anything shows it.
  Users build and install many apps, so this overhead grows with the workspace.
- Measured on running workspace containers (proportional set size per program tree): the root-venv services each keep a 19 MB `uv` parent alive for their whole life (`uv run` does not exec in the pinned uv 0.11.7), a small Flask app costs 35 to 50 MB, and the browser coordinator keeps 43 MB after its window-bound stop of Chromium.
- The shell already has honest liveness: `AppInventory` re-derives `is_running` every 10 seconds from one `getAllProcessInfo` call over supervisord's unix socket (`shell/liveness.py`), plus a TCP connect for rows with no `program`.
- The shell already has idempotent `POST /api/apps/<name>/stop` and `.../start` (contracts section 5.1), refused for a critical app and for any row inside a critical app's program, and refused in a preview shell.
- The window menu offers "Stop X" or "Start X" through those routes (`frontend/src/views/WindowMenu.ts`); a stopped app's window shows a placeholder with a Start button (`Window.ts`), and `livePages.ts` hides the page and reloads it once the app runs again.
- A shortcut or launcher row for a stopped app opens a window showing that placeholder; nothing starts the app.
  `docs/system/specs/rail-shortcuts-and-app-lifecycle.md` says "clicking a shortcut whose backing app is stopped starts it first", which was never implemented; this spec supersedes that sentence (a launch wakes the app through Part B instead).
- The terminal and the browser collect their *resources* (a tmux session, Chromium) once no window shows them (window-bound-resources spec, Part B), told by the shell's close hint (`window_closed_path`) and by a 90 second sweep over `GET /api/desktops`.
  The shell itself destroys nothing.
- `mngr forward` answers a backend it cannot reach with a 503 whose HTML body polls the same URL every second and reloads on any other status; the share gateway's caddy answers a bare 502 for a registered service whose backend is down, and serves its own auto-refreshing loading page only for origins it has no route for.
- Getting Started opens its first-visit window from a thread of its own process, which polls the shell for a connected client (`system/apps/getting_started/src/getting_started/first_window.py`).
  Stopped before anyone has visited, it would never deliver that window.
- The `app-watcher` service is a 40 MB process whose one job is to diff `data/.state/apps.toml` on change and append `service_registered` and `service_deregistered` events to `$MNGR_AGENT_STATE_DIR/events/services/events.jsonl`, which the minds desktop reads to discover an app's origins.
  The shell watches the same file with the same intent (`shell/inventory.py`).
- A scaffolded user app registers itself as the first half of its own program command (`forward_port.py --manifest ... && <entry>`), runs its uv tool entry point directly (no `uv run`), and carries `startsecs = 30` and `startretries = 5`.

## 2. Decisions

1. Neither `mngr forward` nor the share gateway learns how to start an app.
   The workspace guarantees that every registered loopback URL accepts a connection: while an app is stopped, the shell holds its port.
2. The manifest field is `stop_when_no_windows`, a boolean, default `false`, and `critical = true` forces it `false`.
   It governs only the automatic stop of Part D; Quit (Part E) is offered for every stoppable app whatever the field says, and a stopped app always wakes through Part B whatever the field says.
3. Built-in defaults: `files`, `browser`, and `getting-started` declare `true`; `chat`, `terminal`, `terminal-pty`, and `system_interface` are critical.
   The build-app scaffold writes `stop_when_no_windows = true` into every new manifest, so a user app's manifest states the rule outright.
4. The grace period before an automatic stop is 60 seconds, cancelled by any window of the app opening; Quit has none.
5. An app is stopped for having no windows only once a client has arrived at the shell since the shell started.
   Before anyone has looked at the workspace, "no windows" says nothing about use, and the apps that deliver something on the first visit (Getting Started) need to be running for it.
6. User apps keep `autostart = true`: every app registers its row at boot, and the shell stops the ones nobody opens once the workspace is visited.
   Registration therefore stays fused with the program command, and a user app never has to be registered any other way.
7. What a window of a stopped, stoppable app shows is the parker's own loading page, which reloads itself until the app answers.
   The shell's placeholder survives only for rows the shell cannot start (no `program`, or critical).
8. The parker lives in the shell process: the shell already speaks supervisord's RPC, watches the registry, and is critical, so it is the one process guaranteed to be there.
9. A close hint (window-bound-resources section 4.6) is posted only to an app that is running: posting it to a stopped app's port would wake the app to tell it a window closed.
10. The shell announces registrations to the services event file itself, from the registry read it already does; the `app-watcher` program, package, and band go away.
11. The service-start `uv sync` stays for now (bootstrap already runs one; dropping the per-service sync is a later cleanup).

## 3. Part A: no resident `uv run`

`uv run <entry>` stays as the parent of the entry point for the program's whole life.
The three long-lived root-venv services and the one-shot converge run their entry point in place instead:

```ini
command=python3 system/services/oom_priority/bin/oom_tag_service.py host-backup bash -c "uv sync --all-packages --frozen && exec .venv/bin/host-backup"
```

The same shape applies to `share-gateway` and `env-converge` (`exec .venv/bin/env-converge run --phase slow`); `app-watcher` is removed by Part F.
`uv sync --all-packages --frozen` is what `uv run` did first (the root project's rule is never a bare `uv sync`), so a service still comes up over a converged venv; `exec` replaces the bash that supervisord already spawns, so the tagged process is the service itself and its band survives (`oom_tag_service.py` sets the band, then execs, and `execve` keeps it).

The program lines that end in `&& <entry>` without `exec` (`system_interface`, and the build-app scaffold's template and the skill's examples) gain `exec` for the same reason, so no 1 MB bash stays as a parent either.
`with_secrets.py` already execs.

## 4. Part C: `stop_when_no_windows`

### 4.1 The manifest

`app.toml` gains one field (contracts section 2):

| Field | Type | Required | Default | Rule |
|---|---|---|---|---|
| `stop_when_no_windows` | bool | no | `false` | Whether the shell may stop the app's program once no window on any desktop shows it (Part D). `critical = true` forces it `false`; a manifest declaring both fails to load. |

`AppManifest` carries it; its cross-field validator refuses `critical = true` with `stop_when_no_windows = true`.
`forward_port.py --manifest` copies it onto the row like every other boolean the manifest owns (absent in the manifest means absent on the row, which reads as `false`), and `RegistryRow` reads it with the default.

### 4.2 Built-in manifests

| App | `critical` | `stop_when_no_windows` |
|---|---|---|
| `system_interface` | true | false |
| `chat` | true | false |
| `terminal` | true | false |
| `terminal-pty` | true | false |
| `files` | false | true |
| `browser` | false | true |
| `getting-started` | false | true |

`system/test_app_manifests.py` pins the table.
The browser's manifest keeps its `window_closed_path`: Chromium still stops on the close hint, and the coordinator stops 60 seconds later under Part D.

### 4.3 The scaffold

The build-app scaffold's manifest template writes `stop_when_no_windows = true` under `program`, with a comment saying what it does and that `false` keeps the app running for the life of the workspace.
The skill's manifest description names the field beside `priority` and `program`.

### 4.4 The wire

The `app` object of contracts section 5.5 gains `stop_when_no_windows` (a boolean), and `AppRecord` parses it (absent reads as `false`).

## 5. Part B: port parking

### 5.1 What is parked

An app is **stoppable** exactly when the shell's stop route would act on it: its row carries a `program`, it is not critical, and its program is not a critical app's program (the existing rule in `routes.py` and `isAppStoppable`).
An app is **parkable** when it is stoppable and its registered URL names a loopback host (`127.0.0.1`, `localhost`, or `::1`) with an explicit port.
Rows with no `program` (`owner-exec`, the VM exec service, previews) are never parked and never stopped.

### 5.2 The parker

`shell/port_parking.py` holds one `ParkedPort` per parkable app that is stopped: a listening socket on the app's host and port, `SO_REUSEADDR` set, backlog small, accepting on a daemon thread.
On the first accepted connection it:

1. closes its listening socket, so the app can bind the port;
2. asks the lifecycle manager (section 5.4) to wake the app, which calls `startProcess(program, wait=False)` (already-started is success);
3. answers the accepted connection with the loading page (section 5.3) and `Connection: close`, and closes it.

It reads the request only far enough to answer sensibly (the first line and headers, bounded), never proxies anything, and answers every method and path the same way.
Connections left in the backlog when the listener closes are reset by the kernel; whoever sent them sees a connection error and retries (`mngr forward`'s loader polls every second; caddy's error handler serves its own loading page).

Two facts make the handoff safe.
Every failure mode between "the app stopped" and "the app answers" lands on a loading page that retries: a refused connection lands on the proxy's, and a connection the parker accepted lands on the parker's.
And a bind that fails with address-in-use means something is listening on the app's port already, which the parker treats as "running" and leaves alone.

### 5.3 The loading page

The parker answers `503 Service Unavailable` with `Retry-After: 2`, `Cache-Control: no-store`, and an HTML body that says the app is starting and carries `<meta http-equiv="refresh" content="2">`, so a window's frame reloads itself until the app answers.
A fetch from a page gets the same 503 and retries on its own terms.
Nothing here depends on the shell's liveness sweep: the page reloads into the app the moment the app binds.

When the app fails to start (section 5.5), the page instead says the app could not start, names `supervisorctl tail <program> stderr` as where to look, and refreshes every 15 seconds; each refresh is a new request, so the next wake attempt is the user's reload.

### 5.4 The lifecycle manager

`shell/app_lifecycle.py` holds `AppLifecycleManager`, the one owner of every stop, start, park, and wake, with one lock per app so a wake that arrives while a stop is in flight waits for the stop to land and then starts.
It is built by `build_shell_state`, started and stopped with the shell, and disabled outright in a preview shell (which refuses the stop and start verbs already) and in tests unless a test enables it, so no test binds a port by accident.
Its supervisord access is injectable: a states reader (`getAllProcessInfo`, answering each program's `statename`), a start, and a stop, defaulting to the RPC functions of `shell/liveness.py`.

Its sweep runs every 2 seconds while any app is parked and every 10 seconds otherwise, and is woken at once by a window close, a desktop deletion, a window open, a registry read, and a liveness change.
Each pass:

1. reads every supervised program's state in one RPC (none when supervisord cannot be reached: the pass does nothing, logged at debug);
2. for each parkable app whose state is not `RUNNING` or `STARTING` and that is not parked, parks it (a bind refused with address-in-use leaves it unparked);
3. for each parked app whose state is `RUNNING` or `STARTING`, releases the parker (someone started it behind the shell's back, `supervisorctl start` say; its first bind may have failed, and supervisord's retry lands once the port is free, within a few seconds);
4. for each app that was woken and whose state is `FATAL` or `BACKOFF`, re-parks it as failed (section 5.5);
5. applies Part D (section 6).

The existing stop and start routes go through the manager too: a start releases the parker before `startProcess`, so the app's first bind never collides with the shell's listener, and a stop is followed by a liveness refresh so the next pass parks the port.

### 5.5 A wake that fails

A woken app whose program reaches `BACKOFF` or `FATAL` is re-parked with the failure page.
The manager allows at most 3 wake attempts per app in any 5 minutes; past that the parker answers the failure page without starting anything, so a broken app cannot be restarted by every reload.
The budget is per shell process and resets with it.

### 5.6 Launches of a stopped app

A GET launch path needs no special handling: the window's frame requests the page, the parker wakes the app, and the loading page reloads into it.
A POST launch path is asked for its page by the shell itself (`shell/launches.py`), and a POST to the parker would be answered 503.
So before posting a launch to a stoppable app that is not running, the shell wakes it and waits for the app's port to accept a connection (polled, up to 20 seconds; `startsecs` is supervisord's notion of "started" and says nothing about when the app binds), then posts.
An app that does not come up in time fails the launch with the existing `502`.

### 5.7 The share gateway

The rendered Caddyfile gains one site-level block:

```
handle_errors 502 {
    rewrite * /_auth/loading
    reverse_proxy 127.0.0.1:<gateway port>
}
```

A registered service whose backend refuses the connection (the moment between the app stopping and the parker binding, or between the parker releasing and the app binding) then shows the gateway's existing auto-refreshing loading page instead of a bare 502.
The parker's own 503 is a backend answer, not a caddy error, and passes through as it does locally.
The visitor's next request wakes the app through the parker exactly as the owner's does, so a shared app is stoppable too.

## 6. Part D: stop when no windows

### 6.1 The rule

On every pass of the lifecycle sweep, for each app that is stoppable, whose row carries `stop_when_no_windows = true`, and whose program is `RUNNING` or `STARTING`:

- count the windows of the app across every desktop (the owner's and every visitor's; minimized, detached, pinned, linked, and independent alike);
- with at least one window, clear the app's idle mark;
- with none, and once a client has arrived at the shell since it started (decision 5), set the idle mark to now when it is unset, and stop the program when the mark is at least 60 seconds old.

A stop is `stopProcess(program, wait=False)`, logged at info, followed by a liveness refresh; the next pass parks the port.
The idle mark lives in the manager's memory: a shell restart starts the 60 seconds over, which is the safe direction.

The close hint of window-bound-resources section 4.6 still goes out first, from the close itself, so the browser stops Chromium on the close and the coordinator 60 seconds later; the hint is posted only when the app is running (decision 9).

### 6.2 Windows count, not pages

A window that exists but is minimized keeps its app running, though the frontend creates no page for it; the rule is the user's arrangement, not the browser's.
Closing the window is what says the user is done with it.

## 7. Part E: Quit

### 7.1 The route

`POST /api/apps/<name>/quit` (contracts section 5.1): refused as the stop route refuses (preview `403`, unknown `404`, no program or critical `400`), else it closes every window of the app on every desktop through the shell's own close (each close broadcasts and posts the close hint as any close does; a pinned window, which is never closed, stays), then stops the program at once with no grace period, refreshes liveness, and answers `{"name", "is_running"}`.
`POST /api/apps/<name>/stop` and `.../start` remain as they are for agents and tests.

### 7.2 The menu

The window menu's Stop and Start rows become one row, "Quit <display name>" (the power icon), for every stoppable app whether or not it runs (a stopped app's Quit still closes its windows, and the stop is idempotent); a critical app still offers nothing there.
The stopped placeholder loses its Start button: a stoppable app's window never shows the placeholder (decision 7), and the rows that still do cannot be started from the workspace.

### 7.3 The frontend

- `AppRecord` gains `stop_when_no_windows`.
- `isAppStoppable` is unchanged and is also what "wakes through the parker" means in the frontend.
- `Window.ts` shows the placeholder only for a stopped app that is not stoppable; `livePages.ts` holds and reloads a page only for such an app, and treats a stoppable app as showable whatever `is_running` says.
- `ShortcutIcon.ts` dims a stopped app, and says "not running", only when the app is not stoppable.
- `DesktopStore.setAppLifecycle` becomes `quitApp`; `api.ts` posts the quit route.

## 8. Part F: the app watcher moves into the shell

`imbue/system_interface/service_events.py` holds `ServiceEventWriter`: given the rows of a registry read, it writes one `service_registered` event per row whose registered fields (URL, label, icon) differ from the last announced, and one `service_deregistered` per row that left, to `$MNGR_AGENT_STATE_DIR/events/services/events.jsonl`, in the event envelope the watcher wrote (`EventEnvelope` from `imbue_common`, source `services`, nanosecond ISO timestamps, `evt-<uuid>` ids).
The first read after the shell starts remembers nothing and announces every row, which is what a consumer reading the stream from its start needs.
With `MNGR_AGENT_STATE_DIR` unset it logs one warning at start and writes nothing.

`AppInventory` gains an `on_registry_read` hook called with the validated rows after every read; the production shell wires the writer to it, a preview shell wires nothing (its registry is a copy), and tests wire nothing unless they test the writer.
The shell's registry watch is inotify through watchdog, which the watcher's mtime polling backstopped under gVisor and on lima; the inventory's 10 second sweep therefore also compares the registry's mtime and re-reads on a change, so a write no inotify event reported is picked up within the sweep.

Removed: `system/services/app_watcher/` (package, README, changelog), `system/supervisord.conf.d/app-watcher.conf`, the root `pyproject.toml` dependency and workspace source (and the lock's entry), the `app-watcher` band in `oom_priority.bands.SERVICE_BANDS` and its place in the ordering tests, and every mention in docs, comments, and the scaffold's reserved program names.
`app` stays a reserved app name in `forward_port.py` and `app_manifest.primitives` (the reservation costs nothing and an app named `app` would still be a poor name).
`update-self`'s apply handles a removed drop-in as it handles any conf change (`supervisorctl reread && supervisorctl update`), so an existing workspace loses the program at its next update.

The shell still imports nothing from mngr and runs no `mngr` binary: the writer writes a file at a path an environment variable names, in the envelope `imbue_common` defines.

## 9. Contract and document changes

- `contracts.md` section 2: the `stop_when_no_windows` field and the built-in table; section 3: the registry key; section 5.1: the quit route, and the stop and start routes' note that a stopped stoppable app is parked; section 5.3: the close hint goes only to a running app; section 5.5: the `app` object's new key.
- `plan-desktop-interface.md` section 4.5: closing the last window of an app that declares `stop_when_no_windows` stops its program after 60 seconds, and the app comes back on the next request.
- `window-bound-resources.md` section 4.6: the hint is posted only to a running app.
- The shell's README: the lifecycle section (parking, the no-window stop, Quit) and the app watcher's move.
- `system/services/README.md`, `docs/system/workspace-internals.md`, `.agents/shared/worker/references/type-service.md`: the watcher entry goes.
- `system/apps/README.md` and the build-app skill: the field.
- Changelog entries for every project touched.

## 10. Testing

- `app_manifest`: `manifest_test.py` for the field, its default, and the critical refusal; `registry_test.py` for the key; `forward_port_test.py` for the copied key and its clearing.
- The shell, `port_parking_test.py`: a parked port accepts a connection, answers 503 with the loading body and `Connection: close`, releases the port, and reports the wake; a bind on a port something listens on parks nothing.
- The shell, `app_lifecycle_test.py`, over the fake supervisor: a stopped parkable app is parked on a pass and released when its state turns `RUNNING`; a wake starts the program; a `FATAL` after a wake re-parks with the failure page; the wake budget refuses a fourth attempt; the no-window rule stops a `stop_when_no_windows` app only after the grace period, only once a client has arrived, and not while any desktop holds a window of it; an app without the field is never stopped; a critical app is never parked or stopped.
- The shell, `routes_test.py`: the quit route closes every window across desktops and stops the program, refuses a critical app and a preview, and answers `is_running` false.
- The shell, `state_test.py`: the close hint is not posted to a stopped app.
- The shell, `launches_test.py` or `state_test.py`: a POST launch to a stopped stoppable app wakes it first.
- The shell, `service_events_test.py`: the diff rules the watcher's tests pinned (every row on the first read, nothing on an unchanged read, only the changed row, a relabel, a row without a URL), now over `RegistryRow`s; `inventory_test.py`: the hook is called with the rows, and the mtime backstop re-reads.
- The share gateway, `caddyfile_test.py`: the `handle_errors 502` block routes to the loading page.
- The frontend: `WindowMenu.test.ts` (Quit for a stoppable app, running or not; nothing for a critical one), `Window.test.ts` (no placeholder for a stopped stoppable app; the placeholder without a button for an unmanaged one), `livePages.test.ts` (a stoppable app's page is neither held nor reloaded), `records.test.ts` (the parsed field).
- `system/test_app_manifests.py`: the built-in table; `oom_priority`'s band tests without the watcher.
- Manual, on a workspace: close the last File Viewer window, see `supervisorctl status files` go `STOPPED` about a minute later and `ss -ltnp` show the shell holding port 8300; open the File Viewer from the launcher, see the loading page turn into the viewer and `files` back to `RUNNING`; Quit the browser from its window menu and see its windows close and the program stop; share the workspace and open the stopped app from the share link.

## 11. Implementation order

One commit per letter, on one branch, in this order: A, F, C, B, D, E, then the documents.
Each leaves the tree green.
C precedes B, D, and E, which read the field; B precedes D, since a stop without a parker strands the app behind a 503.

## 12. Out of scope

- Merging the agent observer's three mngr processes into one (imbue-ai/mngr-internal#1427).
- A shared host process for the small built-in web apps (imbue-ai/default-workspace-template#733).
- Stopping idle chat agents: a chat's process holds state the shell cannot see (sub-agents as threads, pending callbacks), so it stays with the OOM band mechanism.
- Dropping the per-service `uv sync` that bootstrap already ran.
- Letting the desktop client or `mngr forward` know that an app is parked; both see a backend that answers.
