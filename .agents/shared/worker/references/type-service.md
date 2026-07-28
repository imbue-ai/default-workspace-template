# Type: service

A background service -- a supervisord `[program:<name>]` with no tab. Two
homes: standalone services live under `system/services/<package>/` (their own
uv workspace member, e.g. `host_backup`, `app_watcher`); a service that exists
solely to support one app lives in that app's folder under
`system/apps/<package>/` and is named `<app>-<role>` in
`system/supervisord.conf`.

## Where the source lives

- The package (standalone: `system/services/<package>/`; app-owned: inside the
  app's folder), plus its `pyproject.toml`, `README.md`, and ratchet test.
- The `[program:<name>]` block in `system/supervisord.conf` (see
  `.agents/shared/references/service-processes.md` for the block schema).

## Running and testing

- A fresh worktree has no `.venv`, so run `uv sync --all-packages` once before
  any `uv run`. If a fix needs a new dependency, `uv add ...` and commit the
  manifest changes (`pyproject.toml` / `uv.lock`).
- There is no tab and no frontend: test the service's logic directly with unit
  tests in its package, and exercise its entry point (`uv run <name>`) with a
  bounded invocation where feasible. Never start supervisord, and never
  `supervisorctl` against the served tree from a worktree.
- Verify config-only changes by parsing `system/supervisord.conf` (e.g. with
  Python's `configparser`), not by starting the daemon.

## Working in isolation

Point the service at scratch state (env-var overrides, a copied data dir) --
never at the live workspace's `data/` -- and keep every run bounded so a
long-lived daemon loop cannot outlive the test.
