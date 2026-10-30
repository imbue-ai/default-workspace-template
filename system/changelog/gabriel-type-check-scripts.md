- `system/test_no_type_errors.py` type-checks the root project with ty: the skills and their shared scripts, `system/scripts`, the root-level tests and `conftest.py`. A third party's skill (one whose SKILL.md carries neither `metadata.crystallized: true` nor `metadata.author: imbue`) is excluded from it and from the bare checks. It also checks the bare tier (what the system `python3` runs) a second time, against an environment with no third-party packages. There, an import the system `python3` could not make at runtime shows up as `unresolved-import`, at Python 3.12. The bare check covers the bare packages, the stdlib-only modules of other packages that run under `python3` (such as `notify_user.py`), and every bare skill's script, and it searches exactly the directories a bare file may put on `sys.path`. A bare skill's test, which imports its script by name, is left out of the root check and checked against the root venv with that skill's `scripts/` on the search path. This replaces `stdlib_only_scripts_test.py`, and fixes the errors the new check found.

- The workspace's Python floor is 3.12: `requires-python = ">=3.12"` in the root and every project, and ruff targets 3.12. The build no longer installs a uv-managed 3.12 itself: with no project allowing 3.11, uv can no longer settle on a system 3.11 that the lock rejects, and the build's `UV_PYTHON=3.12` pin has uv download a managed 3.12 when the system has none.

- Local workspaces from before 2026-09-14 (Debian 12, system Python 3.11) can no longer be updated in place (see the update-self change).

- The root launch rules read supervisord program lines the way supervisord does (`command = ...`, `bash -lc "..."`), and a bare-tier file that imports venv-tier first-party code (such as `workspace_scripts`) now fails a root test.

- The workspace-app contract (`contracts.md` section 14) now says a pre-manifest app and the background services run from `.venv/bin/<name>`, not under `uv run`, matching the launch rule.

- `AGENTS.md`'s lockfile rule now says a `uv.lock` change that follows from a `pyproject.toml` change (a dependency or a new skill `python/` project) is committed, not dropped as churn.
