Added `app-manifest select-tests`, which prints the test commands a change calls for, in the order to run them, each with a comment naming the changed paths behind it. It takes a diff (`--diff-base`, `--diff-ref`) or explicit paths (`--path`), and prints shell lines or, with `--format json`, the whole selection.

- Selection reads only what the workspace declares. A path selects its own package's or skill's tests (a collected test file outside both runs alone), and those of every workspace member and npm package that depends on it (from `pyproject.toml`, `package.json` and, for an upgrade in `uv.lock`, the lock's dependency edges). A path in an app's frontend stops at the app itself and its npm consumers, since no Python package that depends on the app loads its bundle. A path also selects the app whose manifest references it or whose program a supervisord block holds, and a change to an app runs the tests of the directories its manifest references.

- Every change except a documentation-only one also runs the always-run set: `system/*.py`, six cross-cutting `system/scripts` guards, and the two checks that read every skill's prose.

- The chat app and the shell run with their browser tests only when they changed themselves, after a frontend build. The chat app's type check runs as a separate command.

- A path in no package or skill (a flat script, an agent hook, repo-level config) runs the full root suite. A consumer the declarations do not show (a script run as a subprocess, a service called over HTTP) is not selected; for an app, declaring the path in its manifest's `[[references]]` selects it.

- A `--diff-base` selection that runs to the checked-out commit refuses to run while the working tree holds uncommitted or untracked changes, and names them: the tests run against the working tree, so a selection read from commits alone would leave those changes untested.

`scope.py` gained `list_changed_files`, `list_tracked_files`, `list_uncommitted_paths`, `resolve_commit`, `read_file_at_revision` and `load_app_manifests`, which `footprint` and `select-tests` share.
