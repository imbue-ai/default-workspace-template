- Skill and reference commands run one-offs as `uv run --no-sync ...`, and the new `.agents/shared/references/running-python.md` gives the rule: one-offs use `uv run --no-sync` unless the instruction says `python3`, a long-running process never runs under `uv run`, and a script's top-level imports stay light.

- The github-sync service's program line and the service-processes example run the entry point from `.venv/bin` after a `--frozen` sync instead of under `uv run`.

- update-self advances the environment snapshot with `uv run --no-sync env-converge upgrade` after an apply, and the build-app scaffold validates a new manifest with `uv run --no-sync app-manifest`.

- `running-python.md` describes where the venv is kept current: a `--frozen` sync at boot, at worker provisioning, and at claude and codex session start, none of which rewrites `uv.lock`.

- update-app's preview launches the app from the worktree the way a supervisord program runs: a `--frozen` sync of the worktree's environment, then the app exec'd from its `.venv`, instead of under `uv run`, so the preview never relocks the worktree and no uv process stays resident as its parent. update-app's docs say to run `uv lock` in the worktree before a `refresh` after a dependency change.

Each skill's Python now lives in its own uv project, `.agents/skills/<name>/python/`: a `pyproject.toml` and a `<skill_name>_skill` package, with tests beside the modules. A skill's scripts run from their modules by path, and `scripts/` keeps shell scripts plus an entry stub only where something outside the release runs a fixed path or the script runs with no venv: launch-task's `create_worker.py`, update-self's two and publish-template's two manifest tools, each saying why it stays. `.agents/shared/scripts` is a project too (package `agents_shared`). A script's imports decide how it runs: one that imports only the standard library (and the bare `workspace_bare_scripts`) runs as `python3 <path>`, with no venv; any other runs as `uv run --no-sync <path>`, and its dependencies resolve in the workspace lock. update-self stays on the system `python3` and still stages as one unit; its entry files put the skill's `python/` on `sys.path`.

- **Scripts that call scripts:** the shared preview script registers its servers through `forward_port` in-process and starts the preview frame by its path under its own interpreter; `preview_app.py` boots, refreshes and tears down previews by calling it in-process; launch-task sends the task and every reply through `message_chat` in-process, and a failed task send is reported with the destroy hint instead of raising; build-app's scaffold validates the manifest with `app_manifest` in-process.

- **Third-party skills are left as they came:** a skill installed or copied in from elsewhere is listed in the new `.agents/third-party-skills.toml` with where it came from, and the import-cost and launch checks leave listed skills out; every other skill is held to them. A `scripts/` of plain or PEP 723 scripts never touches the workspace's uv setup. Built-in skills carry `metadata.author: imbue` (`datalib` and `imbue-studio` now do too).

- **What existing workspaces need:** a workspace's own skills with Python need the same layout. The update worker converts them by following the new `update-self/references/python-packaging-migration.md`, which also covers a skill test that loads its module from a file: it imports the module from the package instead. When a check fails on a skill, the worker first decides from the skill's history, its files and the chat that added it whether the workspace made it, and lists one that came from elsewhere instead of converting it. `migrate-workspace` converts the skills it brings over from an older workspace the same way, and carries over the old workspace's third-party list when it has one.

- **Commands that changed:**
  - `notify_user.py`, `request_secret.py`, `serve_isolated_instance.py`, `resolve_template_base.py` and the preview wrapper import only the standard library, so they run from their modules under `python3`: `.agents/skills/notify-user/python/notify_user_skill/notify_user.py`, `.agents/skills/connect-external-service/python/connect_external_service_skill/request_secret.py`, and `.agents/shared/scripts/agents_shared/` for the rest. The chat's secret card and the secret guards still recognise `request_secret.py` at its new path.
  - `preview_app.py` and `parse_task_frontmatter.py` run from their modules with `uv run --no-sync`: `.agents/skills/update-app/python/update_app_skill/preview_app.py` and `.agents/shared/scripts/agents_shared/parse_task_frontmatter.py`.
  - `refresh_workspace_view.py` and `verify_app.py` (now `system/scripts/workspace_scripts/verify_app.py`) are run with `uv run --no-sync` instead of `python3`.
  - `claude_p.py` lives at `python/use_ai_integration_skill/claude_p.py`.
  - update-self's apply refreshes the open views with `uv run --no-sync`.
  - These run from their modules with `uv run --no-sync`: `.agents/shared/scripts/agents_shared/validate_skill.py`, `.agents/skills/build-app/python/build_app_skill/scaffold_flask_lib.py` and `.agents/skills/migrate-workspace/python/migrate_workspace_skill/migrate_workspace.py`.
  - `datalib`'s `sync_request.py` runs from its module, `.agents/skills/datalib/python/datalib_skill/sync_request.py`, with `uv run --no-sync` instead of `python3`.

- **Bare skills:** a skill whose Python is one script that imports only the standard library can stay a single file, `scripts/<name_with_underscores>.py` (its test, if any, beside it as `scripts/<name_with_underscores>_test.py`), with no `python/` project. It runs as `python3 .agents/skills/<name>/scripts/<name_with_underscores>.py`. The update worker makes a workspace's own single-script, dependency-free skills bare skills.

- **`validate_skill.py`** now requires:
  - for a bare skill, a script that imports only the standard library, has no PEP 723 header, does not shadow a standard-library module, and answers `--help` under `python3 -S -s`;
  - otherwise, a `python/pyproject.toml` naming `<name>-skill` and `<name_with_underscores>_skill` whenever the skill has Python;
  - a passing `uv lock --check`;
  - every entry file, and every package module with its own `if __name__ == "__main__":` block, to answer `--help` under `uv run --no-sync`;
  - no module named like a standard-library module beside a module that runs by its path, which would import it instead.

  It no longer requires PEP 723 headers.

- **New skills:** `.agents/shared/scripts/agents_shared/scaffold_skill.py <name> --description "..." [--python] [--crystallized]` writes a new skill in the spec layout (its `run.py` is a package module run by its path, with no stub in `scripts/`), runs `uv lock` and `uv sync --all-packages`, and validates it. A name or description that would not validate writes nothing.

- **Previews start faster:** `preview_app.py` loads the manifest library (and pydantic) only for `up`; `refresh` and `down` work from the preview's recorded state.

- **Docs:** `spec-summary.md` and `type-skill.md` describe the layout, the scaffold and the lazy-import rule, which has no exceptions: code whose signatures name a heavy library's types goes in a module of its own, imported inside the subcommands that need it. `running-python.md` describes the bare and venv tiers, that a script's imports decide which it is in, and that code calls workspace code by importing it, starting a process only when the call needs one of its own. update-app's `SKILL.md` says a preview syncs its worktree `--frozen` before every boot, so a dependency change there needs `uv lock` before `refresh`.

- **Going live with a merged skill:** a skill with a `python/` project runs from the workspace venv, so `lead-proxy.md`'s provisional go-live and the crystallize, heal and update go-live steps run `uv sync --all-packages --frozen` after the merge.

- **Idle and shed checks on workers:** launch-task imports `chat_background_tasks` (whether a worker is parked on a background task) and `oom_priority` (whether the OOM daemon shed it) as package dependencies of `launch-task-skill`, instead of putting their `src` dirs on `sys.path` from the launcher's checkout. It no longer passes its own checkout when it looks up the marker directory, so `chat_background_tasks` finds it from the checkout the library is installed from.

- **publish-template's GitHub and publish tools:** `github_access.py`, `github_repo.py`, `push_snapshot.py` and `record_publish.py` live in `python/publish_template_skill/` and run in the live workspace's environment, `uv run --no-sync --project /home/user/workspace "$PTM/<tool>.py"`, so they work from the worker's worktree, whose venv the assembly's clean removes. `record_publish.py` imports update-self's ledger helpers from `update_self_skill` instead of putting update-self's scripts on `sys.path`.

- **Python floor:** the root project type-checks every skill's scripts except a third party's skill, and the skill projects require Python 3.12.

- **update-self on an older Python:** the staged `update_self.py` now checks the interpreter before importing anything. On a workspace whose system Python is older than 3.12 (a local workspace from before 2026-09-14), it exits with code 75 and prints the whole procedure: stop the pass, record a `NEEDS_RECREATION` verdict with the workspace's own copy, tell the user it is a migration-required update (`migrate-workspace` moves their work), and release the update lease. Step 3a says only to stop and follow what a failing re-check prints.

- **Migration reference:** a new section on fixing ty errors in a workspace's own skills and apps. The update-self worker fixes them during the update, except an error whose only fix changes behavior the user can see, which it raises with the user instead. The command rewrites also cover the user's scheduled jobs in `data/.state/cron.d/`, which no check scans.

- **Syncing after a merge:** after merging a worker's branch that changed a `pyproject.toml` or `uv.lock` (such as a newly crystallized skill's `python/` project), the lead now runs `uv sync --all-packages --frozen`, so the new package is installed before anything runs it with `uv run --no-sync`. `running-python.md` lists this case. The update-self worker does the same before its validation suites and boots whenever the merge changed a `pyproject.toml` or `uv.lock`, including one only upstream changed, since those suites run `uv run --no-sync`.

- **Harnesses without a session-start sync:** `running-python.md` now says agents on harnesses without a session-start hook (claude and codex have one) sync themselves after a `pyproject.toml` or `uv.lock` change.

- **Ledger pointers:** publish-template, update-installed-template and update-published-template point at `update-self`'s `python/update_self_skill/update_ledger.py` for the exact `VERSION_HISTORY.md` starter block, instead of the `scripts/update_self.py` entry stub.

- **New apps at the 3.12 floor:** build-app's scaffold writes `requires-python = ">=3.12"` into a new app's `pyproject.toml`.

- **update-self's venv step:** an update that changes the backend manifests no longer copies the root venv aside and syncs it in place. It syncs the merged tree into a fresh venv beside the uv cache, through that venv's real path, pre-flights the shell and the chat from it, and switches `.venv` to it just before the restart. The previous venv is deleted once the restart is healthy, and a rollback switches the link back. The first such update on a workspace whose `.venv` is still a real directory converts it in the same step, keeping the old directory until the restart is healthy and putting it back on rollback. A later `rollback-last` re-syncs the venv for the restored tree.

- **`running-python.md`:** `.venv` is a link the workspace manages, with the one command that recreates a missing target.

update-self, build-app and the agent docs move to the app-environments layout: the built-in apps share the workspace venv, and every user-built package is a standalone project.

- **update-self runs and tests the install the services use.** The shell and chat pre-flights boot `.venv/bin/system-interface` and `.venv/bin/chat-app` (or the fresh venv the apply swaps in), the entry points supervisord runs, instead of bare names from the agent's PATH, which found per-app uv tools the services never ran. The per-app tool refresh, snapshots, rollback rebuild and shadowing sweep are gone; a change to a built-in app's `pyproject.toml` re-syncs the venv, and only `uv.lock`, the root `pyproject.toml` and the plugin manifest reinstall the mngr tool. A landed update removes the per-app tools older builds left.

- **User packages are standalone.** A user app, service or lib under `system/` (any package the root `members` does not name) has its own `uv.lock` and `.venv`, so a dependency it adds never moves the chat's versions. The update migrates old-layout ones with update-self's new `user-package-migration` command (`uv run --no-sync user-package-migration migrate --dry-run`, then `migrate --shared-lock <pre-merge lock>`; `relock` for a stale lock): it keeps their versions, turns workspace sources into editable path sources, rewrites their program lines and one-off commands, and takes them out of the root `pyproject.toml`. The worker checks each package's lock, puts the dry run in its report, and gives each converted package its suite and boot from its own venv. An apply syncs each user package it changed in place.

- **Broken user things get fixed after an update, not reported.** After a successful apply, the lead says in one chat line what it is fixing, sends a repair worker with the evidence, and finishes once it is fixed or the worker gives up (still `UPDATED`, with a detail naming what is left).

- **build-app** scaffolds a standalone project: an `app-schema` path dependency, a lock seeded from the root lock, its own test dependencies and pytest root, a linked venv, and a program line that links, syncs `--locked` and execs from its own `.venv/bin`. It no longer installs an app as a uv tool or touches the root lock.

- **Previews and isolated boots** of a user package run from its own venv (`serve_isolated_instance.py --project`). `migrate-workspace` converts the packages it lands (`convert-packages`), and `use-template` and `update-installed-template` say an older template's app needs converting. `AGENTS.md`, `running-python.md`, `service-processes.md`, `spec-summary.md` and the app and service worker guides (`type-app.md`, `type-service.md`) describe the two kinds of package, including where a user package's dependencies change and its tests run.
