---
name: update-app
description: "Use immediately whenever the user asks you to update, change, fix, restyle, extend, restart, or otherwise modify an existing app or background service -- load this BEFORE touching its code and before planning your changes. Applies to any change to an app's or service's backend or frontend logic, or how it runs. Covers both apps (a window the user can open) and background services (host-backup, share-gateway, and other supervisord programs with no window). This is the front door for app and service edits, the workspace's own critical apps (the shell, the chat, the terminal) included: it sizes each request and decides which ones you build yourself and which go to a background worker, it owns the live change loop (apply the change so it takes effect, refresh the user's view, verify) and hands the change to the turn-end hardening flow, and it routes a critical app to the careful flow in references/critical-app.md. For creating a brand-new app use build-app."
metadata:
  author: imbue
---

# Changing an existing app or service

Both apps and background services run as a `[program:<name>]` under
supervisord (one program per file under `system/supervisord.conf.d/`). They differ only in whether
there's a window to refresh:

- **App** -- the user opens it as a window rendering at the service's own
  origin, `http://<name>.<workspace-host>/` (scaffolded via
  `build-app`). Lives under `system/apps/<package>/`.
- **Background service** -- a supervisord program with no window (`host-backup`,
  `share-gateway`, forwarders), standalone under `system/services/` or co-owned
  by an app (named `<app>-<role>`, code in the app's folder).

Two things are easy to forget when editing either, and both leave the user
looking at stale state: a code change doesn't take effect until the process
is reloaded, and an open app window keeps showing the old page until it's
refreshed. The live change loop below handles both.

If you're doing something *other* than editing an existing app or service:

- **Creating a new app** -> `build-app`.
- **Arranging windows** (open/place/focus/minimize/close) -> `manage-desktop`.

## Choose the flow for each change

Run this before touching code, on every message that asks for a change to an
existing app or service. Start at `choose_flow`; it is the only entry point.
`confirm_shape` and `hand_off` are helpers that run only when step 5 of
`choose_flow` calls them for a particular change, so a change that needs
neither (a quick bug fix) never reaches them. Terms used below:

- **live loop**: "The live change loop" section below (make the change, apply
  it, refresh the user's view, verify).
- **worker**: one background worker launched through the `launch-task` skill;
  it builds in its own worktree and follows the build-worker guide
  (`.agents/shared/build-worker/SKILL.md`).
- **few minutes**: the line between building it yourself and handing it off.
  Work longer than that keeps the user's next messages waiting, because they
  wait until you stop.

```
PROCEDURE choose_flow(message)

  1. paths    <- the files the requested changes touch
     manifest <- app.toml of each app that owns them (system/apps/<package>/app.toml)

  2. IF any manifest says critical = true
        (the shell `system_interface`, the chat, the terminal, or any app that declares it)
        OR any path is under system/libs/{workspace_ui, workspace_layout, workspace_themes}/:
         FOLLOW references/critical-app.md for the WHOLE change
           (this includes any ordinary app or skill the same change touches)
         RETURN

  3. changes <- split the message into separate changes
       # A message can ask for several; a change can also arrive alone later,
       # or mixed into feedback on a change already in progress.
       # Size each one as if it had arrived alone. A change that would not have
       # been mocked on its own is never added to another change's mock, and is
       # never held back until that mock is approved.

  4. FOR EACH change IN changes:
       a. SWITCH on how much it changes what the user sees:
            CASE substantially, or you can't tell
                 (redesign, new page or view, look-and-feel shift, new user-facing capability):
                needs_shape <- YES
            CASE little or not at all
                 (bug fix, copy tweak, new field, backend logic, config or `command` change, restart):
                needs_shape <- NO
            CASE in between (e.g. a new control on an existing page):
                decide by how much it changes what the user sees
       b. SWITCH on how long it takes to build, planning and thinking included:
            CASE a few minutes or less:
                builder <- YOU
            CASE longer, typically:
                 - a feature spanning several files or many parts of the app
                 - exploration needing web searches or several sources
                 - reading or writing several pieces of data
                 - processing a medium or large amount of data, or changing many records
                 - a long command (a build, a test suite, a big install)
                 - debugging a problem whose cause is unknown
                builder <- WORKER
       c. WRITE one line in your reply or plan: the change, needs_shape, builder

  5. FOR EACH change, in parallel, without waiting on the others:
       SWITCH (needs_shape, builder):
         CASE (NO, YOU):     GOTO live_loop
         CASE (NO, WORKER):  CALL hand_off(change)
         CASE (YES, YOU):    CALL confirm_shape(change); build it to a usable state; GOTO live_loop
         CASE (YES, WORKER): make a quick mock yourself (confirm_shape step c);
                             CALL hand_off(change) at once, without waiting for confirmation;
                             pass each piece of the user's feedback on the mock to the worker

  INVARIANT: every change reaches the user through the same live loop.
  INVARIANT: a preview or verification may read the live data store, never write to it.


PROCEDURE confirm_shape(change)

  a. READ .agents/shared/references/interactive-delivery.md
  b. IF the change adds a new view or capability to an existing service
        AND the message bundles it with unrelated changes:
         deliver it alone, with its own feedback gate (interactive-delivery phase 8)
  c. IF the change is costly to redo (redesign, touches data, substantial visual shift):
         IF a hand-made throwaway mock would convince the user:
             build the hand mock (Type 2, a detached throwaway)
         ELSE:
             run preview_app.py to show the actually changed app as a labelled
             preview window beside the live one (Type 1; invocation under
             "Protect the user's data while you verify")
     ELSE:
         # a routine tweak, copy change, or behaviour-only change needs no preview:
         # a taste mismatch is cheap to fix next round
         put a cheap throwaway version of the proposed change in front of the user
  d. LOOP: show it, take feedback, revise so the change is visible
     UNTIL the user explicitly confirms the shape
  e. never build heavy against an unconfirmed shape yourself


PROCEDURE hand_off(change)

  1. IF no worker is running for this app:
         commit your pending edits
         launch the worker as `launch-task` describes, with a brief that names:
           - the app
           - the changes handed to the worker, in the user's words
           - the changes you are building yourself, so the worker does not build
             them too (it may still edit the same files)
           - the files and decisions you already know about
           - what done looks like
           - the build-worker guide; do not restate its rules in the brief
     ELSE:
         send the change to the same worker with `create_worker.py reply`
       # One worker takes all long changes. It may combine them and build them
       # together, in whatever order is fastest.

  2. SCOPE RULE for the brief:
       decide the scope yourself, as if you were building it in the chat;
       state what to build, no more and no less, with the context the worker needs
       (the user's words, how the app works now, decisions already made).
       Do not add targets you would not have built yourself.
       Checks that the user's existing data survives still happen before it goes live.

  3. tell the user in one line that the change is being built and that they
     can keep sending requests

  4. WHILE the worker runs:
         handle each new message with choose_flow (short changes through the
         live loop yourself; long ones to the worker with `create_worker.py reply`)
         # you and the worker may edit the same files; conflicts are resolved at merge

  5. ON a milestone or the final report:
         take the service lease (next section)
         merge it as `.agents/shared/references/lead-proxy.md` describes
         apply and refresh it through steps 2 and 3 of the live loop
         tell the user what is now there

  Hardening still happens at turn-end, as "Turn-end" below describes.
  If you ARE that worker: the build-worker guide and your brief override this skill.
```

## One editor at a time: the service lease

Every chat shares this one working tree and this one live process, so two
agents editing the same service at the same time interleave destructively --
there is no merge step where that could be reconciled. Concurrent edits must
be *serialized*, and the serialization token is an advisory lease held as a
regular `tk` ticket (regular tickets are visible across agents).

The exception is a background worker you launched: it is not a second editor
here. It works in its own worktree, so you can keep editing the same files
while it runs; its changes reach the live app only when you merge them, and
any conflicts are resolved in that merge.

**Pre-flight, before touching the service's code or config:**

1. Check whether another agent is mid-edit:

   ```bash
   tk ready > /tmp/service-leases.txt
   grep "editing service <name>" /tmp/service-leases.txt
   ```

   If a lease exists and `tk show <id>` says it is not yours, do **not**
   silently proceed: tell the user another chat is currently modifying this
   service and let them decide (wait, or explicitly override). If the holder
   looks abandoned -- its agent is no longer running, or the lease is hours
   old with no notes -- say that too and offer to break it. The lease is
   advisory: it is broken deliberately by the user's call, never silently.

2. Take your own lease:

   ```bash
   LEASE_ID=$(tk create "editing service <name>" -t chore \
       -d "Held by $MNGR_AGENT_NAME while editing this service; released at turn end.")
   ```

   then `tk start "$LEASE_ID"` (as its own command).

**Release the lease at the end of every editing turn** with
`tk close "$LEASE_ID" "Done editing for this turn."` -- never hold it across
an idle wait for user feedback. On the next feedback round, take a fresh
lease before editing again. Between turns the changes are committed, so
another chat editing sequentially on top is safe; only *simultaneous* editing
needs the lease.

**An in-flight harden pass does not block you -- the foreground wins.** If
`tk ready` also shows an in-progress `update` or `heal` ticket whose title
lists `<name>` among the creations its pass covers (a background pass hardening
an earlier change to this service), proceed with
your edit; your change simply makes that pass stale. Leave a note on that
ticket (`tk add-note <id> "..."`) so its owner catches the pass up to cover
it at merge time. The full contention rules live in
[`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md).

## The live change loop

Make the change interactive and keep the user's view in sync as you go.

### 1. Make the change

Edit the service's code under `system/apps/<package>/` (or wherever the program's
command points). If the change calls Claude,
follow `use-ai-integration` -- the same rules as when the service was
built. Anything the change creates for the app outside its own directory --
a skill that drives it, a script, a doc -- is registered in the app's `app.toml`
under `[[references]]` with a `note` naming the surface it uses, so it travels
with the app through hardening, testing, and publishing.

Check the app's `stop_when_no_windows` against what the change makes it do.
The scaffold writes `true`: the shell stops the program a minute after the
app's last window closes and starts it again only on the next request. If
the change gives the app work between requests -- a poller or file watcher,
a websocket or subscription to an outside service, a job that must finish
after the user closes the window, or an API another agent drives with no
window open -- set it to `false` in `app.toml`, or that work is lost or
interrupted at the stop. Work that runs on a schedule and then exits (a
refresh of the app's data every hour, a nightly sync) is not a reason to
keep the service up: it is a routine with `--app <name>`, made with the
`manage-routines` skill, never a thread or scheduler inside the service.
The reverse holds: an app that no longer does anything between
requests can go back to `true`. The field is read at registration, so the
restart in step 2 is what applies it; `uv run --no-sync app-manifest validate-manifest
system/apps/<package>/app.toml` checks the file.

An app with an `api_schema_path` in its manifest describes its own API there:
an OpenAPI document built from its routes on every request, which is what a
visiting agent reads instead of the code (`build-app`, "The app describes its
own API"). A route the change adds is listed on its own; what the change owes
the document is a docstring on each new or changed view (its first paragraph is
the operation's summary, in the user's terms), an entry in the app's route table
for each new route (`RouteModels` with the pydantic models it reads through
`parse_request_body` and answers through `model_response`, or `SchemalessRoute`
with the reason it exchanges no JSON), the table updated when a route's models
or status change, and an updated `description` in `app.toml` when the change
moves what the app is for. A parse or an answer the table does not name fails
the request, and the app's own test (`runner_test.py` in a scaffolded app) fails
when the document and the manifest disagree, a view has no docstring, or a
route is missing from the table, so run it with the change.

### 2. Apply it so it actually takes effect

The scaffolded web runner runs with `use_reloader=False`, and daemons
don't watch their own source, so a code change is **not** live until the
process restarts:

- **Backend change** (Python / server logic, for an app or a
  daemon): restart the program. For an app, steps 2 to 4 of this loop are
  one call -- it restarts without waiting out the program's 30 s `startsecs`
  window, waits for the port, checks a marker, and refreshes or opens the
  window:

  ```bash
  uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart --marker "<text unique to your change>" --refresh
  ```

  With no window open yet, pass `--open` instead of `--refresh`: the call
  opens the window only after the checks pass, the order step 3 asks for.

  While the app is still framed as a mock (`build-app` keeps a new app in a
  labeled mock frame until it reads its real data, and `uv run --no-sync
  workspace-layout list` then lists `<name>-sketch` in its place), end this
  call, or the dependency change's below, with `&& uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py refresh --app <name>` in place of
  `--refresh`: it re-frames the app with the frame's own note (keeping the
  restarted app out of the launcher) and reloads the mock window. With no
  window open yet, end it with `mock_frame.py up --app <name> --open`
  instead. Once a framed app reads its real data, take the frame down as
  `build-app` Step 2 says.

  A daemon with no port cannot use `verify_app.py` (it needs a port to
  check), so bounce it with supervisord directly; the call waits out the
  program's `startsecs`, which is 30 s only if its block sets `startsecs=30`:

  ```bash
  supervisorctl restart <name>
  supervisorctl status <name>   # confirm it came back RUNNING
  ```

- **Dependency or entry-point change to an app** (its `pyproject.toml`): an
  app's venv is an editable install of `system/apps/<package>/` that picks up
  source edits on its own but not a new dependency or console script. A
  user-built app is a standalone project with its own lock and venv: change its
  dependencies in its own directory, then restart (its program syncs its venv
  from its lock at start):

  ```bash
  (cd system/apps/<package> && uv add <dependency>)   # or `uv lock` after a hand edit
  uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart --marker "<text>" --refresh
  ```

  That touches only the app's own `pyproject.toml`, `uv.lock` and venv, never
  the root lock or the chat's environment. A built-in app runs from the root
  venv instead: `uv lock` and `uv sync --all-packages` at the repo root, then
  restart. (A user service under `system/services/` works like a user app: `uv
  add` in its directory, then `supervisorctl restart <name>`.)

- **Frontend-only change** (templates, static JS/CSS served fresh on each
  request): no restart needed -- the next request already serves the new
  markup. Skip straight to the refresh.

- **Change to the service *definition*** (its port, its `command`, its log
  config, or adding/removing a program): edit the program's own
  `system/supervisord.conf.d/<name>.conf`, then
  `supervisorctl reread && supervisorctl update`. The full program schema,
  the add/remove/inspect mechanics, and the `forward_port.py` wiring live
  in [`.agents/shared/references/service-processes.md`](../../shared/references/service-processes.md).
  This surgical reload is for iterating on a single service. It is *not* the
  path for landing an `update-self` merge -- that restarts the whole services
  agent (`mngr start --restart system-services`) so `bootstrap` re-runs too,
  and must be followed by
  `uv run --no-sync system/scripts/refresh_workspace_view.py` (see step 3).

If it doesn't come back `RUNNING`, read
`/var/log/supervisor/<name>-stderr.log` or
`supervisorctl tail <name> stderr`.

### 3. Refresh the user's view

If the service has a user-facing window, the open page is still showing the
pre-change content. Refresh it so the user sees the update without being told
to click Refresh:

```bash
uv run --no-sync workspace-layout refresh --app <name>
```

While the app is still framed as a mock (`workspace-layout list` lists
`<name>-sketch`, as step 2 says), the user's window is the frame's: reload it
with `uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py refresh --app
<name>` instead, and never `open` the app itself. With no window open yet,
surface the frame with `uv run --no-sync
.agents/skills/build-app/python/build_app_skill/mock_frame.py up --app <name>
--open`, which keeps the frame's note.

`refresh --app` reloads every page of the service on every client. If no
window is open yet and the change is ready to show, surface it instead with
`uv run --no-sync workspace-layout open <name>`: with no `--desktop` it lands
on the desktop the user is looking at, and `--desktop <name>` targets one
desktop (and switches the client to it).
**That `open` puts the window on the user's screen the moment it returns** -- it
is the act of showing them, so only run it on something you are ready for them to
see, and never follow it by telling them to open the window. What makes you ready
is step 4: with no window open yet you are still in the private window it asks
for, so **run step 4's verification before this `open`, not after it** -- this is
the one branch of the loop where the numbered order and that rule disagree, and
the rule wins. (A window that was already open gives you no such window; see
step 4.) For any other window manipulation, see `manage-desktop`. Background
daemons have no window -- skip the refresh, but not the rest of this step.

If you restarted the whole services agent rather than a single program, one
window refresh is not enough -- the workspace shell itself was bounced. Rebuild
the user's whole view instead:

```bash
uv run --no-sync system/scripts/refresh_workspace_view.py
```

Nothing else does this for you. The Imbue Studio app only intervenes when a workspace
looks unreachable for a sustained stretch, and a services restart that comes
back quickly never crosses that bar, so the user is left reading the page the
previous build rendered. The helper is fire-and-forget and always exits 0; it
names any channel that did not land on stderr and is never a reason to stop.

### 4. Verify

Confirm the change actually does the right thing, exercised as the user
would (not just "the process is up"):

- **App**: the `verify_app.py --restart --marker ... --refresh` call of
  step 2 is this check (HTTP against the registered backend URL
  `http://127.0.0.1:<port>/`, the marker unique to your change, and
  `--screenshot` for a rendered page only with `--open`, before any window
  exists); add one `curl` of a changed route to the same call when the change
  is an API. The recipe is in
  `build-app`'s [verify reference](../build-app/references/verify.md);
  the symptom-indexed gotchas (connection refused, a window stuck on the
  loading page, broken WebSockets) are in that skill's
  `cross-flow-gotchas.md`.
- **Daemon**: watch its log (`supervisorctl tail -f <name> stderr`) and
  confirm the new behavior actually fires.

**Verify before the user can see it, not after.** This step belongs in the
window where you are the only one looking -- before the window exists, or against a
throwaway instance of your own (below). An open window closes the first of those
before you get to step 3: the service you would be driving is the one already in
front of them, and refreshing it changes what they can see, not whose it is. Once a
surface is in front of the user, poking at it yourself is both redundant and
wrong: they are the verifier for anything they can perceive, and driving a
service they are watching means your test actions land in their view and, for
anything wired to real data or real agents, in their state. `build-app` orders
it this way for exactly this reason -- verify is its Step 3, surfacing the window
its Step 4.

So if the change is already surfaced when you finish it, the honest sequence is
apply -> restart -> refresh -> *tell them what changed*, and the checking you do
against the live service is the cheap kind that cannot touch their view: a
`curl`, a health probe, an exit code, a log line. Save the thorough pass for the
turn-end harden worker, which runs against its own instance.

### Protect the user's data while you verify

The service's persistent store -- `data/.apps/<name>/` (whatever `DATA_DIR`
resolves to) -- **is the user's real data**. The recurring, expensive
failure mode is not the code edit: it is *verifying* a change by writing
test data into the live store and then "cleaning up" with a delete/reset
whose predicate is too broad and takes real records with it. The delete is
where the data dies. Encode these, cheapest first:

- **Read-only verification needs no ceremony.** Most changes (UI, copy, a
  backend read path) can be exercised by curl/Playwright against the live
  service without writing anything. Reading the live store -- including to
  *render* a preview -- is fine; the danger is only writes. That is the *data*
  question, and answering it does not settle the timing one above. Read-only
  still means a browser drive belongs in the private window: once a window is open,
  a read-only Playwright pass writes nothing but still lands in the user's view,
  and the live service is yours only for a `curl` or a health probe.

- **If exercising the change must write, mutate, or delete data, never
  point it at the live store.** Boot a throwaway instance against a *copy* of
  the store on a *spare* port, exercise it there, then tear it down with its
  copy. The shared
  [`serve_isolated_instance.py`](../../shared/scripts/agents_shared/serve_isolated_instance.py)
  script owns all of it -- `--copy` copies the store into the instance's own
  space on disk, it picks a free port, injects it (via the
  `<PACKAGE_UPPER>_PORT` override) plus your data-dir override pointed at the
  copy, waits for the instance to answer, and prints its URL; `down` stops it
  and deletes the copy:

  ```bash
  URL=$(uv run --no-sync .agents/shared/scripts/agents_shared/serve_isolated_instance.py up \
      --name <name>-test --cwd . \
      --port-env <PACKAGE_UPPER>_PORT \
      --copy data=data/.apps/<name> \
      --env '<PACKAGE_UPPER>_DATA_DIR={copy:data}' \
      --health-path /health \
      --project system/apps/<package> \
      -- <name>)
  # ...exercise the change at "$URL" (curl / Playwright); it can write freely...
  uv run --no-sync .agents/shared/scripts/agents_shared/serve_isolated_instance.py down --name <name>-test
  ```

  `--project` boots a user-built app (a standalone project with its own lock and
  venv) from its own venv, synced first, as its program does; drop it, and run
  `-- .venv/bin/<name>`, for a built-in app, which runs from the root venv.

  **Never copy app data into `/tmp`.** In the workspace container `/tmp` is a
  small RAM disk (about 1 GiB): a copy of any real store fails part way with
  "No space left on device", and the partial copy fills `/tmp` for everything
  else until you delete it. `--copy` writes to disk, outside the backup, and
  refuses a copy that would not leave the disk room to spare. If it refuses,
  copy only what the test needs (`--copy` a subdirectory, or seed a small store
  for the test) or verify read-only.

  **To pick up a further edit, refresh in place -- don't tear down and re-`up`.**
  A `down`/`up` cycle picks a new port, so a surfaced preview window would point at
  a dead one. `refresh` re-boots just the instance's own process on its existing
  port, leaving the port, the service registration, and any window untouched:

  ```bash
  uv run --no-sync .agents/shared/scripts/agents_shared/serve_isolated_instance.py refresh --name <name>-test
  ```

  A change that only alters files the running process reads from disk on each
  request needs no refresh at all. Reserve `down` for when you are finished.

  This is the point of the `DATA_DIR` + `<PACKAGE_UPPER>_PORT` overrides: the
  isolation you need is **data isolation, not code isolation**, and it's a
  one-command setup, not a worktree. The live store is only ever
  *read* (once, to make the copy); the only delete lands on a disposable path
  where real data never lived.

  **An app with an `app.toml` needs none of that spelled out.** Its manifest's
  `[preview]` table (the scaffold writes one; absent, the scaffold convention
  applies) says how a throwaway instance boots, and one script boots it from a
  worktree, on free ports, over a scratch copy of the directories the table
  names, surfaced as the labeled `<name>-preview` app:

  ```bash
  uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py up \
      --app <name> --worktree <dir>          # prints <name>-preview
  uv run --no-sync workspace-layout open <name>-preview   # puts a window of it in front of the user
  uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py refresh --app <name>   # after a rebuild, in place
  uv run --no-sync .agents/skills/update-app/python/update_app_skill/preview_app.py down --app <name>
  ```

  Every `up` and `refresh` syncs the app's venv in the worktree and then starts
  the app from it, so the preview serves the worktree's code: a built-in app
  from the worktree's root `.venv` (`uv sync --all-packages --frozen`), a
  user-built app from its own project's (`uv sync --locked`). That boot sync
  counts against the preview's health wait, so on a fresh `git worktree add`
  run the same sync there once before the first `up`. The sync never relocks:
  after changing a dependency in the worktree, run `uv lock` (in the app's
  directory, for a user-built app) before `refresh`, or a built-in app's
  preview boots without the change and a user-built app's fails its `--locked`
  sync.
  If the app has a frontend, build it in the worktree too.

  `--with <sibling>` boots a sibling app's preview from the same worktree first
  and points the app's copied registry at it: a shell preview frames the
  previewed chat that way, and a terminal preview needs `--with terminal-pty`,
  since its pages frame the pty the registry names;
  `--instance-key <key>` names the instance the window opens on, for an app
  whose `open_path` takes one (a chat opens on a conversation). The worktree is
  the app's code isolation when the change is one the user must see before it
  lands; for a contained change exercised against a data copy, the raw
  `serve_isolated_instance.py` call above, from the live tree, is still the
  cheaper shape, and the only shape for a service with no manifest. Either way
  the preview's window is the same labeled frame.

- **Never "clean up" test data by deleting from the live store.** If you
  did leave a stray test record in it, leave it -- an additive junk record
  is a far cheaper mistake than a broad delete. Better: don't write to the
  live store in the first place (use the copy above).

- **Snapshot before any genuinely in-place change to the real store.** If a
  change truly must rewrite the live store (a data migration you can't run
  on a copy), snapshot it to disk under `/var/tmp` first, run the change,
  confirm the real data survived, and only then remove the snapshot:

  ```bash
  du -sh data/.apps/<name>; uv run --no-sync disk-headroom /var/tmp    # the copy must leave several GB free
  cp -a data/.apps/<name> /var/tmp/<name>-pre-<change>
  # ...run the change, confirm the real data survived...
  rm -rf /var/tmp/<name>-pre-<change>
  ```

  If the copy would not leave the disk room to spare, free space first or ask
  the user before changing the live store without one -- never snapshot into
  `/tmp` instead.

  The snapshot is a *recovery net* -- do **not** turn it into a routine
  "wipe live and restore backup" step: overwriting a running service's store
  tears its state, and any real writes that landed during your test window
  are silently lost on restore.

- **Retrofit older services when you touch them.** A service that predates
  this convention hardcodes `data/.apps/<name>/` and its listen port at its call
  sites. Add both overrides the scaffold now emits, as part of your change, so
  the throwaway instance above works: the data-dir override
  `DATA_DIR = Path(os.environ.get("<PACKAGE_UPPER>_DATA_DIR", "data/.apps/<name>"))`
  (route reads/writes through it), and the port override
  `PORT = int(os.environ.get("<PACKAGE_UPPER>_PORT", "<assigned-port>"))`
  (bind `PORT` in `run_simple`, never a hardcoded literal). If you genuinely
  can't, fall back to read-only verification plus the snapshot net.

- **The copy isolates local state, not external effects.** Pointing at a
  data copy does not stop a test run from really posting to Slack, calling a
  remote API, or sending a message. Guard those separately (a dry-run flag,
  test credentials) -- the data copy only protects the local store.

## Removing a service

Dropping a service is the definition-level case of step 2: remove its
`[program:<name>]` block, `supervisorctl reread && supervisorctl update`,
and (for an app) `python3 system/scripts/forward_port.py --name <name>
--remove` plus reverting the scaffolded lib. The mechanics are in
[`.agents/shared/references/service-processes.md`](../../shared/references/service-processes.md); for a
scaffolded web lib, `build-app`'s `cleanup.md` reference has the
full teardown.

Teardown stops at the code and the process. **Leave the service's data
(`data/.apps/<name>/`) in place** -- removing a service is not license to
delete the user's records. Delete the data dir only if the user explicitly
asks, and confirm before you do.

## Turn-end: get feedback, then harden the change

The live loop above delivers the change to the user interactively. At
turn-end, formalize it through the background worker pipeline -- the main
agent never runs the thorough test passes or the review gates itself.

**Get the user's feedback before you start any hardening pass.** Delivering
the change live is not the same as the user *wanting* it, so never dispatch
the hardening worker in the same turn you make the change. This holds for
*every* change, contained or larger-scope -- even a one-line copy tweak gets
shown and confirmed first. It can take **several rounds**: treat each
response as another live iteration -- make the change, show it, ask again --
and hold the harden pass until you are sure the user is satisfied. A single
"looks fine" mid-thread while they're still tweaking isn't done. (For a
larger-scope change this gate is the *working* result, not just the mock,
exactly as `build-app`'s Step 5 gates on the working site.)

- **A change you and the user discussed and applied live, or repeatable work you
  did by hand** -> invoke `update-creation` once for the whole change, listing
  every creation it touched: each app as `app:<name>`, each background service
  as `service:<package>`, and any skill the same change edited as
  `skill:<name>`. Two apps changed to work together are one pass with one
  worker, never one per app. It opens a tracking ticket, dispatches the generic
  harden worker to verify/test the change on its own branch, proxies the gates,
  merges, and refreshes each window on go-live.
- **The service errored or produced a wrong result and you worked around
  it** -> invoke `heal-creation` at turn-end instead, again once, covering
  every creation the failure spans (`app:<name>`, or `service:<package>` for
  a background service).
- **A critical app** (the shell, the chat, the terminal, or a user app whose
  manifest says so) never reaches this step from here: its live loop, harden
  handoff, and go-live through the atomic update apply are all
  [`references/critical-app.md`](references/critical-app.md)'s.

`update-creation` and `heal-creation` also stand on their own as turn-end
skills; this skill's turn-end step is just the service-shaped entry into
them.

Both flows enforce single-flight per creation: if another chat already has a
harden pass in flight for any creation in yours, they leave a note on its
ticket instead of dispatching a sibling, and that pass catches up at merge
time to cover both changes. See
[`.agents/shared/references/harden-contention.md`](../../shared/references/harden-contention.md).
