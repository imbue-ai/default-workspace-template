Every skill's `scripts/` dir (and `.agents/shared/scripts`) is now a uv project. The code lives in a `<skill_name>_skill` package (`agents_shared` for the shared scripts), and every existing script path stays as a thin entry file. Skill scripts run as `uv run --no-sync <path>`, and their dependencies resolve in the workspace lock. update-self stays on the system `python3` and still stages as one unit.

- **What existing workspaces need:** a workspace's own skills with scripts need a `scripts/pyproject.toml` and the same layout. The update worker converts them by following the new `update-self/references/python-packaging-migration.md`.

- **Commands that changed:**
  - `notify_user.py`, `request_secret.py`, `preview_app.py`, `serve_isolated_instance.py`, `refresh_workspace_view.py` and `smoketest_app.py` are now run with `uv run --no-sync` instead of `python3`.
  - `claude_p.py` lives at `scripts/use_ai_integration_skill/claude_p.py`.

- **`validate_skill.py`** now requires:
  - a `scripts/pyproject.toml` naming `<name>-skill` and `<name>_skill`;
  - a passing `uv lock --check`;
  - every entry file to answer `--help` under `uv run --no-sync`.

  It no longer requires PEP 723 headers.

- **Docs:** `spec-summary.md` and `type-skill.md` describe the package layout and the lazy-import rule. `running-python.md` describes the bare and venv tiers.
