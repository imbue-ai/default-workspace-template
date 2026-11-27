# bootstrap

First-boot setup for a default-workspace-template host, followed by launching
[supervisord](http://supervisord.org/), which supervises every background
service.

## CLI

- `bootstrap` - Run first-boot setup, then `exec` supervisord in the foreground.
  Invoked once per container boot from the `bootstrap` extra_window (see
  `.mngr/settings.toml`).

## What it does

`bootstrap` runs, in order:

1. **Global git config** - rewrites `git@`/`ssh://` GitHub remotes to `https://`.
   (`core.hooksPath` is deliberately NOT set here: the post-commit auto-push
   hook only becomes active when the opt-in github-sync skill wires it up --
   see `system/libs/github_sync/README.md`.)
2. **Workspace main branch** - on first boot only (gated by
   `data/.state/workspace_main_branch_initialized`), commits the rsynced workspace
   onto a clean `main` branch. It creates no chat: the chat app creates chats on
   demand. The same first boot writes `data/.state/system_interface/offer_tour`,
   which tells the system interface the workspace is new, so its show-around tour
   may start by itself; a workspace that already existed never gets it. It also
   writes `data/.state/chat/owes_welcome_chat`, which tells the chat app to open
   the workspace's welcome chat once it is running.
3. **Initial data** - on every boot, once the workspace's Python packages are
   in place and before the clock is set, runs `apply-initial-data`
   (`system/libs/workspace_initial_data`), which applies the document whoever
   created the workspace left at `$MNGR_HOST_DIR/initial_data.json`: the profile
   snapshot, the timezone, the update settings and the backup destination. The
   document being there is the trigger, not a first boot: a pool host's first
   boot is over when it is baked, and its document is written when it is
   adopted, before its services are started again. A boot that finds none does
   nothing. A document a part of which cannot be applied is left for the next
   boot, which tries that part again and writes no part a second time, and the
   boot goes on; `GET /api/workspace/status` says how it stands.
4. **Retired registrations** - removes the app registry's `owner-exec` and
   `vm-exec` rows and `data/.state/owner_exec_inner.toml`. No program of this
   release registers them, so a workspace updated from a release that ran those
   programs would otherwise keep them, and a row keeps an origin on the share
   with nothing behind it.
5. **Launch supervisord** - `exec supervisord -n -c system/supervisord.conf`. Running
   via `exec` keeps the bootstrap tmux window alive as supervisord and lets the
   supervised services inherit this shell's already-sourced agent environment.
   That environment gets the workspace venv activated (`.venv/bin` first on
   `PATH`, `VIRTUAL_ENV` set), so a program line that names its entry point bare
   resolves the venv's console script.

### Progress events

When `MNGR_AGENT_STATE_DIR` is set (always, inside an agent), the boot announces
each step it runs by appending one JSON line under the standard event envelope to
`$MNGR_AGENT_STATE_DIR/events/bootstrap/events.jsonl`: a `bootstrap_step_started`
and a `bootstrap_step_completed` (carrying `duration_seconds`) per step, each with
the step's stable `step` id and a human `label`, then a final `bootstrap_completed`
as supervisord is exec'd. The Imbue Studio desktop tails these
(`mngr event <agent> bootstrap --follow`) to move its create progress bar through
the boot, so the step ids are a contract: reword a label freely, but keep a step's
id once shipped. A step that raises is never reported completed. The file is
appended to on every boot of the workspace and never cleared, so an adopted pool
host's opens with the bake's own boot ahead of the user's; a reader interested in
the boot in progress follows the whole file and keeps only the events whose
`timestamp` falls after it began waiting (the desktop filters against the moment
its create started, with an allowance for clock skew), so the stamp on every event
is part of the contract too. Writing the events is best effort and never fails
the boot; without `MNGR_AGENT_STATE_DIR` (a bare dev run) nothing is written.

## Services (supervisord)

Services are defined as `[program:*]` sections, one program per file under
`system/supervisord.conf.d/` (pulled in by an `[include]` glob in
`system/supervisord.conf`) at the repo
root, not managed by this package. supervisord starts them, restarts the
long-lived ones when they exit (`autorestart=true`), and runs one-shot programs
(like `deferred-install`) exactly once per boot (`autorestart=false`).

Services inherit the agent environment from the bootstrap shell that exec'd
supervisord (there is no per-service `environment=` enumeration). Each program
writes separate, rotated, container-local logs under
`/var/log/supervisor/<name>-stdout.log` and `<name>-stderr.log` (not under
`runtime/`, so they are not backed up).

To add, change, or remove a service, add/edit/delete its own
`system/supervisord.conf.d/<name>.conf` and run
`supervisorctl reread && supervisorctl update`. To bounce an app after a code
change use `uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart`, which
does not wait out a scaffolded app's `startsecs=30`; `supervisorctl restart
<name>` is for a daemon with no port. See the `update-app` skill, or
`.agents/shared/references/service-processes.md`, for details.

## Environment convergence (env-converge)

Package deferral now lives in `system/services/env_converge`: the one-shot `env-converge`
supervisord program runs every `system/scripts/env.d/<NNNN>-<name>.sh` unit (each
idempotent with a fast satisfied-check -- no marker files) and converges the
rootfs back to the environment record at the pinned apt snapshot timestamp.
Bootstrap's role is only the fast phase: it applies the overlay symlinks from
`system/scripts/env.d/overlay-paths.json` synchronously before exec'ing supervisord,
so no service ever writes to a rootfs path that should persist.

Heavy packages not needed by boot-time services (currently the Screen's stack
with the neko binary, and the Fortress browser + its Chromium apt libs, all in
`system/scripts/env.d/2000-screen.sh`) install this way on first boot. If something
tries to use one before its unit has finished, it fails loudly -- that is
acceptable. Check `supervisorctl status env-converge`,
`/var/log/supervisor/env-converge-stdout.log`, or the concrete satisfied
condition (e.g. `test -x /opt/fortress/tilion-fortress/tilion`) before using
browser automation in a fresh workspace. See `system/services/env_converge/README.md`
for the full contract.
