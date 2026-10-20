This release turns the workspace's scripts into packages. An existing workspace's own skills, services and docs are migrated during the update by following `.agents/skills/update-self/references/python-packaging-migration.md`.

- `system/scripts` is one uv project with two packages. `workspace_bare_scripts` runs under the system `python3` with no venv and imports only the standard library. `workspace_scripts` (the changelog gate, the docs viewer, the view refresh and the app smoketest) runs from the root venv.

- A script path stays as a thin entry file that imports its package where the script runs with no venv or something outside this release calls it by path: the bare scripts, `refresh_workspace_view.py`, update-self's and launch-task's `create_worker.py`. The changelog gate, the docs viewer and the app smoketest, which only this release calls, run from their modules: `system/scripts/workspace_scripts/<name>.py`. The smoketest no longer re-runs itself under `.venv/bin/python` when playwright is missing; it says to run it with `uv run --no-sync`.

- The four guard hooks (pipe into tail/head, latchkey request, secrets, tk standalone) run their checks through one entry, `system/scripts/agent_hook_check.py <check>`, instead of a wrapper each. Only it, the rewrite hook's entry, the two background-task entries and the skill entry files that run with no venv (update-self's, and publish-template's two manifest tools) still edit `sys.path`.

- `background_tasks.py`, `claude_background_task_hooks.py` and `vendor_mngr_skill.py` move into `workspace_bare_scripts`, with their tests beside them. Each keeps its path as an entry file, since all three run by that path under the system `python3`: Imbue Studio and its admin and eval tools run `background_tasks.py`, Claude's Stop and SessionStart hooks run `claude_background_task_hooks.py`, and CI and mngr's pin bump run `vendor_mngr_skill.py`. `system/test_external_entry_paths.py` now runs all three. The two background-task entry files put `system/libs/chat_background_tasks` on `sys.path` (the hooks' entry also puts `oom_priority` there, for the rewrite hook's command parsing), so they are counted among the import-path edits. The modules import the library normally.

- `forward_port` has a `register()` / `remove()` API, which its command line now runs, and `message_chat` a `send()`. The shared preview script registers its servers and launch-task sends its messages through them in-process instead of starting `python3` on the scripts.

- The root workspace adds the `system/scripts`, `.agents/shared/scripts` and `.agents/skills/*/python` members, so skill dependencies resolve in the one `uv.lock`.

- The CI changelog job runs the gate (`system/scripts/workspace_scripts/check_changelog_entries.py`) from a `uv sync --frozen --package workspace-scripts` venv.

- New root checks:
  - `system/test_entry_point_import_cost.py` fails an entry point -- an entry file, or a package module with its own `if __name__ == "__main__":` block -- that loads pydantic, loguru, click, tenacity, httpx or asyncio at import without declaring it. A heavy module that another one it loads imports counts toward that one, so declaring pydantic also covers the asyncio pydantic loads on Python 3.12.
  - `system/test_external_entry_paths.py` runs every path that Imbue Studio, CI and older update-self releases call by path. The bare paths run with no site-packages; the one venv-tier path, launch-task's `create_worker.py`, runs from the root venv.
  - `system/test_python_launch_rules.py` fails a `python3 <path>.py` that names a venv-tier or missing script, and a `uv run <path>.py` whose script is not in the tree, so a path that moved is rewritten wherever it is written down. It also counts, tests included, the code that edits the import path (`sys.path` under any alias, `monkeypatch.syspath_prepend`, `site.addsitedir`, a non-empty `PYTHONPATH` handed to a subprocess, a pytest `pythonpath` setting) and the code that loads a module from a file (`spec_from_file_location`, `spec_from_loader`, the file loaders, `runpy.run_path`, `exec(compile(...))`), and fails when either count rises. Skills without a `python/` project, such as third-party skills, are not counted.
  - The plain-`uv run` check no longer reads files that link into `system/vendor/`. Once the mngr assets were fetched, `docs/system/style_guide.md` resolved to mngr's style guide and failed the check inside a workspace.
  - The plain-`uv run` check now covers script runs too: a `uv run <path>.py` with no `--no-sync` fails it, and so does a Python argv that starts `uv run` with a computed program and no `--no-sync` before it.
  - `system/test_meta_ratchets.py` requires a `pyproject.toml` in every skill `python/` dir. Without one, every `uv` command in the workspace fails.

- The Datalib app's launcher, `system/apps/datalib/launch_datalib_http.py`, counts as a bare entry point, like oom_priority's own scripts: its supervisord program runs it under the system `python3`, so the `sys.path` insert it uses to import `oom_priority` is one of the counted import-path edits. Its tests import it by name instead of loading it from its file.

- The bug-report collector runs as a `Collector` object that takes its container paths and class budgets as arguments. Its tests build one per test instead of loading a fresh copy of the module by path. What the collector collects is unchanged.

- The supervisord program readers in `system/test_app_manifests.py` and `system/test_supervisord_layout.py` share one helper, `oom_priority.supervisord_testing.supervisord_commands`, instead of loading each other's test files by path. The root checks that skip vendored content share `system/vendor_testing.py`.

- `system/scripts/migrate_claude_auth.py` is deleted. It was a manual, one-time move of Claude keys out of the mngr host env file, from when auth moved into accounts. Without it, `workspace-scripts` no longer depends on the chat app or mngr, so chat's tests can depend on it without a dependency cycle.

- The gcp and azure create templates no longer forward `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` and `GH_TOKEN` into the workspace's host env. Every other template stopped in July (Claude auth moved into accounts, and GitHub sync became an opt-in skill); these two were added on a parallel branch and kept the old line.

- `system/scripts/stdlib_only_scripts_test.py` holds `oom_priority` to the stdlib-only rule as it does `chat_background_tasks`; it had been exempt from the check.
