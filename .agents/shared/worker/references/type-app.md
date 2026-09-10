# Type: app

An app -- a scaffolded Flask lib under `system/apps/<package>/`, registered in
`system/supervisord.conf`, served at its own browser origin
(`http://<name>.<workspace-host>/`, routed straight to the app's registered
port). This reference describes what an app *is*; for how to
run and test a web frontend in isolation, see
`.agents/shared/worker/references/web-frontend-testing.md`.

## Where the source lives

An app's footprint is its own directory, the `system/supervisord.conf` sections
that run it, and the `[[references]]` its `app.toml` declares -- the skills,
scripts, and docs built for the app that live elsewhere in the tree. The scope file
(`harden-creation.md`) is that footprint resolved to literal paths.

- The scaffolded lib: `system/apps/<package>/src/<package>/runner.py` (the Flask app
  and routes), plus its `pyproject.toml`, `README.md`, and
  `test_<package>_ratchets.py`. This is the scope file's `primary`.
- The app's program entry in `system/supervisord.conf` and the matching root
  `pyproject.toml` workspace wiring -- you normally do not touch these; the
  scaffold created them.
- A background service co-owned by the app (a `<app>-<role>` program) also
  lives in the app's folder; treat it as part of the app, and its
  `[program:<app>-<role>]` block as one more `wiring` section beside the app's
  own.
- The manifest's `[[references]]`: the skills, scripts, and docs built for this
  app that sit outside its directory, each with a `note` naming the surface it
  uses. A change that lands outside the footprint -- `diff.outside_footprint` in
  the scope file names it -- is registered here when the file belongs to the
  app, rather than left unclaimed.

## Running and testing

The isolated-instance and rendered-page rules are in `web-frontend-testing.md`.
App specifics:

- A fresh worktree has no `.venv`, so run `uv sync --all-packages` once before
  any `uv run`. If a fix needs a new dependency, `uv add ...` and commit the
  manifest changes (`pyproject.toml` / `uv.lock`).
- Add a `test_<package>.py` for the routes, and test the whole footprint rather
  than the app directory alone -- a referenced skill's tests have to run when
  the app's surface moves. The app is its own project with its own pytest and
  coverage configuration, so it runs from its own root; the referenced paths
  outside it are covered by the root configuration and run from the repo root:

  ```bash
  cd system/apps/<package> && uv run pytest    # primary, plus test_<package>_ratchets.py
  ```

  ```bash
  # from the repo root, over every referenced directory that holds tests
  # anywhere beneath it (a skill keeps its tests under scripts/):
  REFERENCE_TEST_DIRS=$(jq -r '.references[].path' "$SCOPE_FILE" | while read -r p; do
      [ -d "$p" ] && [ -n "$(find "$p" -name '*_test.py' -o -name 'test_*.py' | head -1)" ] && echo "$p"
  done)
  [ -n "$REFERENCE_TEST_DIRS" ] && uv run pytest $REFERENCE_TEST_DIRS
  ```

  The guard on `REFERENCE_TEST_DIRS` matters: a bare `uv run pytest` from the
  repo root runs the whole monorepo suite, vendored code included. Every
  `primary` directory gets its own project-root run when the creation has more
  than one; a referenced file, or a directory with no tests beneath it, is left
  out of the run, and the ratchet file of any referenced project that has one is
  run too. A pre-manifest app carries no scope file, so the app-directory run is
  its whole test set.

## Working in isolation

Beyond the live-instance rules in `web-frontend-testing.md`: do not run `layout.py
open` / `refresh` / `list` against the served tree, and do not touch
`system/apps/system_interface`.
