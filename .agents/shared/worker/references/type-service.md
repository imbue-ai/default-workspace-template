# Type: service

A background service -- a supervisord `[program:<name>]` with no tab. Two
homes: standalone services live under `system/services/<package>/` (a built-in
one is a root uv workspace member, e.g. `host_backup`, `share_gateway`; a
user-built one is its own uv project); a service that exists
solely to support one app lives in that app's folder under
`system/apps/<package>/` and is named `<app>-<role>`.

## Where the source lives

- The package (standalone: `system/services/<package>/`; app-owned: inside the
  app's folder), plus its `pyproject.toml`, `README.md`, and ratchet test.
- The `[program:<name>]` block in its own `system/supervisord.conf.d/<name>.conf`
  (see `.agents/shared/references/service-processes.md` for the block schema).
  One file per program, so adding or removing a service never edits a file
  another creation also owns. A standalone service's program is named after
  its package with underscores as hyphens (`host_backup` runs as
  `host-backup`), and any further program it runs is `<name>-<role>`: the scope
  file's `wiring` and the update apply's rollback find its programs only by
  those names. `oom_priority`'s programs (`earlyoom`, `publish_cgroup_meminfo`,
  `resource_sampler`, and the `oom-tag-backstop` event listener) are named
  otherwise, so its `wiring` is empty: an edit to their blocks lands in
  `outside_footprint`, and a rollback leaves restarting them to the agent. A
  service a routine runs (the Caretaker's check, run by
  `system/routines/caretaker.toml`) has no program, so its `wiring` is empty
  too: an edit to its routine's definition lands in `outside_footprint` (name
  it under `Outside footprint:`), and nothing needs restarting, since its next
  run reads the merged code.

## Running and testing

- A user-built service is a standalone uv project
  (`.agents/shared/references/running-python.md`, "User packages are standalone
  projects"): the worktree's setup syncs its venv from its own lock (`uv sync
  --locked` in its directory does it again), and a new dependency is `uv add
  ...` **in its directory**, committing its own `pyproject.toml` and `uv.lock`,
  never the root's. A built-in service is a root workspace member: a fresh
  worktree has no root `.venv`, so run `uv sync --all-packages` once before any
  `uv run`, and `uv add` at the root. An app-owned service shares its app's
  package and follows the app (`type-app.md`).
- There is no tab and no frontend: test the service's logic directly with unit
  tests in its package, and exercise its entry point with a bounded invocation
  where feasible -- a user-built service's from its own venv (`uv run --no-sync
  --project system/services/<package> <name>`), a built-in one's from the root
  venv (`uv run --no-sync <name>`). Never start supervisord, and never
  `supervisorctl` against the served tree from a worktree.
- While you iterate, run the package's tests, never a bare root `uv run
  --no-sync pytest`. A user-built package's suite runs from its own directory,
  against its own venv: the root pytest configuration skips user packages, so a
  run by path from the repo root collects nothing.

  ```bash
  (cd system/services/<package> && uv run --no-sync pytest)   # a user-built service
  uv run --no-sync pytest system/services/<package>           # a built-in service
  ```

  An app-owned service's tests are its app's suite (`type-app.md`).

  The gate is `harden-creation.md`'s "The test gate", which adds whatever
  consumes the package.
- Verify config-only changes by parsing the service's own
  `system/supervisord.conf.d/<name>.conf` (e.g. with Python's `configparser`),
  not by starting the daemon. To check the whole realized set instead, read
  `system/supervisord.conf` through supervisord's own `ServerOptions`, which
  follows the `[include]` glob -- `configparser` does not.

## Working in isolation

Point the service at scratch state (env-var overrides, a copied data dir) --
never at the live workspace's `data/` -- and keep every run bounded so a
long-lived daemon loop cannot outlive the test.
