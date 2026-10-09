Each skill's Python now lives in its own uv project, `.agents/skills/<name>/python/`: a `pyproject.toml` and a `<skill_name>_skill` package, with tests beside the modules. `scripts/` keeps only thin entry files at every existing path, plus shell scripts. `.agents/shared/scripts` is a project too (package `agents_shared`). Skill scripts run as `uv run --no-sync <path>`, and their dependencies resolve in the workspace lock. update-self stays on the system `python3` and still stages as one unit; its entry files put the skill's `python/` on `sys.path`.

- **Third-party skills are unaffected:** a skill whose `scripts/` holds plain or PEP 723 scripts never touches the workspace's uv setup.

- **What existing workspaces need:** a workspace's own skills with Python need the same layout. The update worker converts them by following the new `update-self/references/python-packaging-migration.md`.

- **Commands that changed:**
  - `notify_user.py`, `request_secret.py`, `preview_app.py`, `serve_isolated_instance.py`, `refresh_workspace_view.py` and `smoketest_app.py` are now run with `uv run --no-sync` instead of `python3`.
  - `claude_p.py` lives at `python/use_ai_integration_skill/claude_p.py`.
  - update-self's apply refreshes the open views with `uv run --no-sync`.

- **`validate_skill.py`** now requires:
  - a `python/pyproject.toml` naming `<name>-skill` and `<name>_skill` whenever the skill has Python;
  - a passing `uv lock --check`;
  - every entry file to answer `--help` under `uv run --no-sync`.

  It no longer requires PEP 723 headers.

- **Docs:** `spec-summary.md` and `type-skill.md` describe the layout and the lazy-import rule. `running-python.md` describes the bare and venv tiers.
