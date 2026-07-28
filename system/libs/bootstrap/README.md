# bootstrap

First-boot setup for a default-workspace-template host, followed by launching
[supervisord](http://supervisord.org/), which supervises every background
service.

## CLI

- `bootstrap` - Run first-boot setup, then `exec` supervisord in the foreground.
  Invoked once per container boot from the `bootstrap` extra_window (see
  `.mngr/settings.toml`).

## What it does

`uv run bootstrap` runs, in order:

1. **Global git config** - rewrites `git@`/`ssh://` GitHub remotes to `https://`.
   (`core.hooksPath` is deliberately NOT set here: the post-commit auto-push
   hook only becomes active when the opt-in github-sync skill wires it up --
   see `system/libs/github_sync/README.md`.)
2. **CLAUDE_CONFIG_DIR host-env write** - records the services agent's per-agent
   Claude config dir in `$MNGR_HOST_DIR/env` so every other agent on the host
   inherits it.
3. **Initial chat agent** - on first boot only (gated by
   `data/.state/initial_chat_created`), commits the rsynced workspace onto a clean
   `main` branch and creates the welcome chat agent (`--message /welcome`).
4. **Launch supervisord** - `exec supervisord -n -c system/supervisord.conf`. Running
   via `exec` keeps the bootstrap tmux window alive as supervisord and lets the
   supervised services inherit this shell's already-sourced agent environment.

## Services (supervisord)

Services are defined as `[program:*]` sections in `system/supervisord.conf` at the repo
root, not managed by this package. supervisord starts them, restarts the
long-lived ones when they exit (`autorestart=true`), and runs one-shot programs
(like `deferred-install`) exactly once per boot (`autorestart=false`).

Services inherit the agent environment from the bootstrap shell that exec'd
supervisord (there is no per-service `environment=` enumeration). Each program
writes separate, rotated, container-local logs under
`/var/log/supervisor/<name>-stdout.log` and `<name>-stderr.log` (not under
`runtime/`, so they are not backed up).

To add, change, or remove a service, edit `system/supervisord.conf` and run
`supervisorctl reread && supervisorctl update` (and `supervisorctl restart
<name>` to bounce one). See the `update-app` skill, or
`.agents/shared/references/service-processes.md`, for details.

## Environment convergence (env-converge)

Package deferral now lives in `system/services/env_converge`: the one-shot `env-converge`
supervisord program runs every `system/scripts/env.d/<NNNN>-<name>.sh` unit (each
idempotent with a fast satisfied-check -- no marker files) and converges the
rootfs back to the environment record at the pinned apt snapshot timestamp.
Bootstrap's role is only the fast phase: it applies the overlay symlinks from
`system/scripts/env.d/overlay-paths.json` synchronously before exec'ing supervisord,
so no service ever writes to a rootfs path that should persist.

Heavy packages not needed by boot-time services (currently the Fortress
browser + its Chromium apt libs) install this way on first boot. If something
tries to use one before its unit has finished, it fails loudly -- that is
acceptable. Check `supervisorctl status env-converge`,
`/var/log/supervisor/env-converge-stdout.log`, or the concrete satisfied
condition (e.g. `test -x /opt/fortress/tilion-fortress/tilion`) before using
browser automation in a fresh workspace. See `system/services/env_converge/README.md`
for the full contract.
