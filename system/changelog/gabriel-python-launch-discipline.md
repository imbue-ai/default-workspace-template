- The `bootstrap` window, github-sync's program line and the user-service example in `service-processes.md` now start their entry point from `.venv/bin` after a `uv sync --all-packages --frozen`, as host-backup, share-gateway and env-converge already did, so no `uv` process stays resident as a parent of supervisord or any program. A failed boot-time sync still starts `bootstrap` on the existing venv, so a workspace with a broken dependency tree stays reachable.

- The `SessionStart` hook runs `system/scripts/session_start_sync.sh`: a `uv.lock` that has drifted from `pyproject.toml` is regenerated once (and the agent is told to include it in its next commit), then the venv is synced `--frozen`.

- One-off commands in `AGENTS.md`, config and scripts use `uv run --no-sync`, so following an instruction never relocks or syncs the workspace. The new root test `system/test_python_launch_rules.py` fails on any supervisord program (user-added drop-ins included) that runs under `uv run`, and on any plain `uv run` in a tracked doc, prompt, config or script; an existing workspace's own services and docs get rewritten during the update merge.

- The secrets guard recognises `uv run --no-sync` / `--frozen` / `--quiet` in front of `with_secrets.py` and `request_secret.py`; any other `uv run` option in front of them is still blocked.
