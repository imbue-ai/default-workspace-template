Each skill's Python now lives in its own uv project, `.agents/skills/<name>/python/`: a `pyproject.toml` and a `<skill_name>_skill` package, with tests beside the modules. `scripts/` keeps thin entry files where something outside the release calls a fixed path or the script runs with no venv, plus shell scripts; a built-in script only this release calls runs from its module by path. `.agents/shared/scripts` is a project too (package `agents_shared`). Skill scripts run as `uv run --no-sync <path>`, and their dependencies resolve in the workspace lock. update-self stays on the system `python3` and still stages as one unit; its entry files put the skill's `python/` on `sys.path`.

- **Scripts that call scripts:** the shared preview script registers its servers through `forward_port` in-process and starts the preview frame with `python -m agents_shared.preview_wrapper_server`; `preview_app.py` boots, refreshes and tears down previews by calling it in-process; launch-task sends the task and every reply through `message_chat` in-process, and a failed task send is reported with the destroy hint instead of raising; build-app's scaffold validates the manifest with `app_manifest` in-process.

- **Third-party skills don't break uv:** a skill whose `scripts/` holds plain or PEP 723 scripts is not a uv workspace member, so it can't break `uv` commands in the workspace. The root import-cost and type checks still cover its scripts.

- **What existing workspaces need:** a workspace's own skills with Python need the same layout. The update worker converts them by following the new `update-self/references/python-packaging-migration.md`, which also covers a skill test that loads its module from a file: it imports the module from the package instead. `migrate-workspace` converts the skills it brings over from an older workspace the same way.

- **Commands that changed:**
  - `notify_user.py`, `request_secret.py`, `serve_isolated_instance.py`, `refresh_workspace_view.py` and `smoketest_app.py` are now run with `uv run --no-sync` instead of `python3`, and `preview_app.py` with `uv run --no-sync` instead of `uv run python3`.
  - `claude_p.py` lives at `python/use_ai_integration_skill/claude_p.py`.
  - update-self's apply refreshes the open views with `uv run --no-sync`.
  - These run from their modules: `.agents/shared/scripts/agents_shared/validate_skill.py`, `.agents/shared/scripts/agents_shared/resolve_template_base.py`, `.agents/skills/build-app/python/build_app_skill/scaffold_flask_lib.py` and `.agents/skills/migrate-workspace/python/migrate_workspace_skill/migrate_workspace.py`.
  - `datalib`'s `sync_request.py` runs from its module, `.agents/skills/datalib/python/datalib_skill/sync_request.py`, with `uv run --no-sync` instead of `python3`.

- **`validate_skill.py`** now requires:
  - a `python/pyproject.toml` naming `<name>-skill` and `<name_with_underscores>_skill` whenever the skill has Python;
  - a passing `uv lock --check`;
  - every entry file, and every package module with its own `if __name__ == "__main__":` block, to answer `--help` under `uv run --no-sync`;
  - no module named like a standard-library module beside a module that runs by its path, which would import it instead.

  It no longer requires PEP 723 headers.

- **Docs:** `spec-summary.md` and `type-skill.md` describe the layout and the lazy-import rule. `running-python.md` describes the bare and venv tiers. update-app's `SKILL.md` says a preview syncs its worktree `--frozen` before every boot, so a dependency change there needs `uv lock` before `refresh`.

- **Going live with a merged skill:** a skill with a `python/` project runs from the workspace venv, so `lead-proxy.md`'s provisional go-live and the crystallize, heal and update go-live steps run `uv sync --all-packages --frozen` after the merge.

- **Idle and shed checks on workers:** launch-task imports `chat_background_tasks` (whether a worker is parked on a background task) and `oom_priority` (whether the OOM daemon shed it) as package dependencies of `launch-task-skill`, instead of putting their `src` dirs on `sys.path` from the launcher's checkout. It no longer passes its own checkout when it looks up the marker directory, so `chat_background_tasks` finds it from the checkout the library is installed from.
