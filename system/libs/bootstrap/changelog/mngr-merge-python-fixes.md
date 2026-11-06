`bootstrap` runs the env-converge fast phase with `uv run --no-sync`. In a newly created workspace, its window starts it from `.venv/bin` after a `--frozen` sync instead of under `uv run`; a workspace created earlier keeps the `uv run bootstrap` window mngr recorded when it created it. It execs supervisord with the workspace venv first on `PATH` and `VIRTUAL_ENV` set, as `uv run` used to leave it, so supervisord's programs still resolve their entry points (a user-built app's included) in the venv.

The README names the app check by its new path and launch, `uv run --no-sync system/scripts/workspace_scripts/verify_app.py <name> --restart`.

The project requires Python 3.12 or newer (was 3.11), matching the workspace floor: every workspace already runs 3.12, and the lock already required it.

The docstring of the environment bootstrap hands supervisord now gives its current reason (bare-named commands such as the agent observer's `mngr` resolve to the workspace venv first), since every app's program line names its entry point by path and no app has a uv tool of its own. No behavior change.
