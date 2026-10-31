`select-tests` prints its test and type-check commands as `uv run --no-sync ...`, so running them never relocks or syncs the workspace.

`select-tests` names the always-run guard tests at their new paths inside the `agents_shared` and `launch_task_skill` packages.

The README points at the name-rule drift test at its new path, `system/scripts/workspace_bare_scripts/forward_port_test.py`.

The project requires Python 3.12 or newer (was 3.11), matching the workspace floor.

`select-tests` no longer names `system/scripts/stdlib_only_scripts_test.py` among the always-run guards; the root `system/test_no_type_errors.py`, which runs with every `system/*.py`, replaces it.

`select-tests` runs a skill's `python/` project as part of the skill's own suite: a change that reaches it (a library it depends on, a `uv.lock` upgrade) selects `.agents/skills/<name>` once, instead of the skill and its `python/` directory as two runs of the same tests.

`select-tests` treats every user package (a standalone project under `system/` that the root workspace does not name) as its own pytest root, so its tests run from its directory against its own venv, and they still run when a library it depends on by path changes. A changed Python package whose own suite has no tests now gets an import check, run in that package's environment, so a change nothing would otherwise load (an undeclared import, say) is still caught.
