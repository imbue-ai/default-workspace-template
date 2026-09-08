# Type: app

An app -- a scaffolded Flask lib under `system/apps/<package>/`, registered in
`system/supervisord.conf`, served at its own browser origin
(`http://<name>.<workspace-host>/`, routed straight to the app's registered
port). This reference describes what an app *is*; for how to
run and test a web frontend in isolation, see
`.agents/shared/worker/references/web-frontend-testing.md`.

## Where the source lives

- The scaffolded lib: `system/apps/<package>/src/<package>/runner.py` (the Flask app
  and routes), plus its `pyproject.toml`, `README.md`, and
  `test_<package>_ratchets.py`.
- The app's program entry in `system/supervisord.conf` and the matching root
  `pyproject.toml` workspace wiring -- you normally do not touch these; the
  scaffold created them.
- A background service co-owned by the app (a `<app>-<role>` program) also
  lives in the app's folder; treat it as part of the app.

## Running and testing

The isolated-instance and rendered-page rules are in `web-frontend-testing.md`.
App specifics:

- A fresh worktree has no `.venv`, so run `uv sync --all-packages` once before
  any `uv run`. If a fix needs a new dependency, `uv add ...` and commit the
  manifest changes (`pyproject.toml` / `uv.lock`).
- Add a `test_<package>.py` for the routes, and run `cd system/apps/<package> && uv run
  pytest` (or the repo-root invocation the project uses) plus the ratchets in
  `test_<package>_ratchets.py`.

## Working in isolation

Beyond the live-instance rules in `web-frontend-testing.md`: do not run `layout.py
open` / `refresh` / `list` against the served tree.

## Critical apps

The shell (`system/apps/system_interface`), the chat (`system/apps/chat`), the
terminal (`system/apps/terminal`), and any app whose `app.toml` says
`critical = true` reach you only through the careful flow's handoff
(`op-update.md`, "Critical-app handoff"): the lead built the change in a
worktree, previewed it to the user, and handed you the branch at approval.
What differs for you:

- **Build both bundles at the npm workspace root** (`cd system && npm ci && npm
  run build`), never one frontend alone: the shell's and the chat's bundles are
  built from the shared `system/libs/workspace_ui/` library, and the apply
  installs the bundles you built. Report both `static/` paths in your `done`
  body, so the lead can pass them to the apply.
- **Never drive `networkidle`** against a shell or chat instance in Playwright:
  both hold sockets open for as long as they run, so the wait never returns.
  Wait for the element you are about to read instead.
- **Verify against your own instance.** Boot it with `preview_app.py`
  (`.agents/skills/update-app/scripts/preview_app.py up --app <name> --worktree
  <your work_dir>`) or the raw isolated-instance script; the live app and its
  tabs are the user's.
- The go-live is the lead's: the atomic update apply, after your `done`. You
  neither merge nor restart anything in the served tree.
