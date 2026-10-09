- `system/test_no_type_errors.py` type-checks the root project with ty: the skills and their shared scripts, `system/scripts`, the root-level tests and `conftest.py`. It also checks the bare tier (what the system `python3` runs) a second time, against an environment with no third-party packages. There, an import the system `python3` could not make at runtime shows up as `unresolved-import`, at Python 3.12. This replaces `stdlib_only_scripts_test.py`, and fixes the errors the new check found.

- The workspace's Python floor is 3.12: `requires-python = ">=3.12"` in the root and every project, and ruff and pyright target 3.12. The build no longer installs a uv-managed 3.12 or pins `UV_PYTHON`, because uv picks a 3.12 itself now that nothing allows 3.11.

- Local workspaces from before 2026-09-14 (Debian 12, system Python 3.11) can no longer be updated in place (see the update-self change).

- A worker's provision-time sync is `--frozen`, so a lock that drifted from `pyproject.toml` is regenerated only by the worker's SessionStart hook, which tells the agent to commit it. The root launch rules also read supervisord program lines the way supervisord does (`command = ...`, `bash -lc "..."`), and a bare-tier file that imports venv-tier first-party code (such as `workspace_scripts`) now fails a root test.
