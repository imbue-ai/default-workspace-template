Added `app-manifest select-tests`, which prints the test commands a change calls for, in the order to run them, each with a comment naming the changed paths behind it. It takes a diff (`--diff-base`, `--diff-ref`) or explicit paths (`--path`), and prints shell lines or, with `--format json`, the whole selection.

- A path selects its own package's, skill's, or paired script's tests, and those of every workspace member and npm package that depends on it (from `pyproject.toml`, `package.json` and, for an upgrade in `uv.lock`, the lock's dependency edges). It also selects the app whose manifest references it or whose supervisord block it holds, every test file that names it, and what the new override file (`system/config/test_selection_overrides.toml`) says.

- Every change except a documentation-only one also runs the always-run set: `system/*.py` and six cross-cutting `system/scripts` guards.

- The chat app and the shell run with their browser tests only when they changed themselves, after a frontend build. The chat app's type check runs as a separate command.

- A path nothing classifies brings in the full root suite and is listed, so a gap in the mapping costs time rather than coverage.

- `test_repo_test_selection.py` holds the tree to the mapping: it fails when a tracked path that is not documentation maps to no suite, or when the override file names a suite that does not exist. CI runs it on every change; the fix is a mapping in the override file.

`scope.py` gained `list_changed_files`, `list_tracked_files`, `read_file_at_revision` and `load_app_manifests`, which `footprint` and `select-tests` share.
