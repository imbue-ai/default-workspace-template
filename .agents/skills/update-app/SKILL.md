---
name: update-app
description: "Use immediately whenever the user asks you to update, change, fix, restyle, extend, restart, or otherwise modify an existing app or background service -- load this BEFORE touching its code. Applies to any change to an app's or service's backend or frontend logic, or how it runs. Covers both apps (a window the user can open) and background services (host-backup, share-gateway, and other supervisord programs with no window). This is the front door for app and service edits, the workspace's own critical apps (the shell, the chat, the terminal) included: it owns the change mechanics (apply the change so it takes effect) and routes a critical app to the careful flow in references/critical-app.md. For creating a brand-new app use build-app."
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

A code change doesn't take effect until the process is reloaded; the live
change loop below handles that.

If you're doing something *other* than editing an existing app or service:

- **Creating a new app** -> `build-app`.

## Critical apps

**First, find the app and read its manifest.** Locate the code the change
touches and the `app.toml` of the app that owns it (`system/apps/<package>/app.toml`).
If it says `critical = true` -- the shell (`system_interface`), the chat, the
terminal, and any user app that declares it -- or if the change is under
`system/libs/workspace_ui/` (the shared library both critical bundles are built
from) or `system/libs/workspace_layout/` (the layout wire contract the shell, the
chat, and the terminal all run), **follow [`references/critical-app.md`](references/critical-app.md) and
stop reading here.** A critical app is never edited in the served tree: that
flow runs the same live loop against an isolated worktree, with a preview window
as the user's view, and goes live through the atomic update apply once a
background worker has hardened the change. Everything below is for an app or
service that is not critical.

## The live change loop

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
the change gives the app work between requests -- a background thread or
scheduler that refreshes data, a poller or file watcher, a websocket or
subscription to an outside service, a job that must finish after the user
closes the window, or an API another agent drives with no window open --
set it to `false` in `app.toml`, or that work is lost or interrupted at the
stop. The reverse holds: an app that no longer does anything between
requests can go back to `true`. The field is read at registration, so the
restart in step 2 is what applies it; `uv run app-manifest validate-manifest
system/apps/<package>/app.toml` checks the file.

### 2. Apply it so it actually takes effect

The scaffolded web runner runs with `use_reloader=False`, and daemons
don't watch their own source, so a code change is **not** live until the
process restarts:

- **Backend change** (Python / server logic, for an app or a
  daemon): restart the program.

  ```bash
  supervisorctl restart <name>
  supervisorctl status <name>   # confirm it came back RUNNING
  ```

- **Dependency or entry-point change to an app** (its `pyproject.toml`):
  an app with an `app.toml` manifest runs from its own uv tool environment,
  an editable install of `system/apps/<package>/` that picks up source
  edits on its own but not a new dependency or console script. Reinstall
  the tool, then restart (an app with no `app.toml` runs from the root
  venv: for it, only the `uv sync --all-packages` below is needed):

  ```bash
  uv tool install -e system/apps/<package> --reinstall
  uv sync --all-packages            # the app is also a workspace member; keep the lockfile current
  supervisorctl restart <name>
  ```

  (Background services under `system/services/` run from the root venv
  instead: `uv sync --all-packages`, then restart.)

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
  `python3 system/scripts/refresh_workspace_view.py`.

If it doesn't come back `RUNNING`, read
`/var/log/supervisor/<name>-stderr.log` or
`supervisorctl tail <name> stderr`.

### 3. Checking it (optional)

Checking the change is optional. If you do, `curl` the registered backend
URL `http://127.0.0.1:<port>/`, or watch a daemon's log
(`supervisorctl tail -f <name> stderr`). If you restarted the whole services
agent rather than a single program, `python3 system/scripts/refresh_workspace_view.py`
rebuilds the user's view.

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
  *render* a preview -- is fine; the danger is only writes.

- **If exercising the change must write, mutate, or delete data, never
  point it at the live store.** Boot a throwaway instance against a *copy* of
  the store on a *spare* port, exercise it there, then tear it down with its
  copy. The shared
  [`serve_isolated_instance.py`](../../shared/scripts/serve_isolated_instance.py)
  script owns all of it -- `--copy` copies the store into the instance's own
  space on disk, it picks a free port, injects it (via the
  `<PACKAGE_UPPER>_PORT` override) plus your data-dir override pointed at the
  copy, waits for the instance to answer, and prints its URL; `down` stops it
  and deletes the copy:

  ```bash
  URL=$(python3 .agents/shared/scripts/serve_isolated_instance.py up \
      --name <name>-test --cwd . \
      --port-env <PACKAGE_UPPER>_PORT \
      --copy data=data/.apps/<name> \
      --env '<PACKAGE_UPPER>_DATA_DIR={copy:data}' \
      --health-path /health \
      -- uv run <name>)
  # ...exercise the change at "$URL" (curl / Playwright); it can write freely...
  python3 .agents/shared/scripts/serve_isolated_instance.py down --name <name>-test
  ```

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
  python3 .agents/shared/scripts/serve_isolated_instance.py refresh --name <name>-test
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
  uv run python3 .agents/skills/update-app/scripts/preview_app.py up \
      --app <name> --worktree <dir>          # prints <name>-preview
  uv run python3 .agents/skills/update-app/scripts/preview_app.py refresh --app <name>   # after a rebuild, in place
  uv run python3 .agents/skills/update-app/scripts/preview_app.py down --app <name>
  ```

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
  du -sh data/.apps/<name>; df -h /var/tmp    # the copy must leave several GB free
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
