# Applying a change, by kind

Read this when step 4's change is more than a code edit to an app: a
dependency, a service with no port, the program's own definition, or a restart
of the whole services agent.

A scaffolded app runs with `use_reloader=False`, and a service does not watch
its own source, so a change to Python code is **not** live until the process
restarts.

## A dependency or entry-point change to an app

An app's venv is an editable install of `system/apps/<package>/`. It picks up
source edits on its own, but not a new dependency or console script.

- **A user-built app** is a standalone project with its own lock and venv.
  Change its dependencies in its own directory, then restart; its program
  syncs its venv from its lock at start:

  ```bash
  (cd system/apps/<package> && uv add <dependency>)   # or `uv lock` after a hand edit
  uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart --marker "<text>" --refresh
  ```

  Nothing shared moves: the rules for a user-built package's dependencies are
  in `.agents/shared/references/running-python.md`, "User packages are
  standalone projects".
- **A built-in app** runs from the root venv: `uv lock` and
  `uv sync --all-packages` at the repo root, then restart.
- **A user-built service** under `system/services/` works like a user-built
  app: `uv add` in its directory, then `supervisorctl restart <name>`.

## A service with no port

`verify_app.py` needs a port to check, so a service without one is bounced
with supervisord directly. The call waits out the program's `startsecs`, which
is 30 s only if its block sets `startsecs=30`:

```bash
supervisorctl restart <name>
supervisorctl status <name>   # confirm it came back RUNNING
```

Then verify it by watching its log (`supervisorctl tail -f <name> stderr`) and
confirming the new behaviour actually fires. A service has no window, so there
is nothing to refresh.

## A change to the program's definition

For a change to the program itself (its port, its `command`, its log config, or
adding or removing a program), edit the program's own
`system/supervisord.conf.d/<name>.conf`, then:

```bash
supervisorctl reread && supervisorctl update
```

The full program schema, the add, remove and inspect mechanics, and the
`forward_port.py` wiring are in the shared service-process reference that
SKILL.md's step 4 names.

This surgical reload is for iterating on a single program. It is *not* the
path for landing an `update-self` merge: that restarts the whole services
agent (`mngr start --restart system-services`) so `bootstrap` re-runs too.

## After restarting the whole services agent

If you restarted the whole services agent and not a single program, one window
refresh is not enough: the workspace shell itself was bounced. Rebuild the
user's whole view instead:

```bash
uv run --no-sync system/scripts/refresh_workspace_view.py
```

Nothing else does this for you. The Imbue Studio app intervenes only when a
workspace looks unreachable for a sustained stretch, and a services restart
that comes back quickly never crosses that bar, so the user is left reading
the page the previous build rendered. The helper is fire-and-forget and always
exits 0. It names any channel that did not land on stderr, and that is never a
reason to stop.

## When the program does not come back

If the program is not `RUNNING` after the restart, read
`/var/log/supervisor/<name>-stderr.log` or run
`supervisorctl tail <name> stderr`.
