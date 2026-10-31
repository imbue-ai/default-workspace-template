The project requires Python 3.12 or newer (was 3.11), matching the workspace floor.

`select-tests` no longer names `system/scripts/stdlib_only_scripts_test.py` among the always-run guards; the root `system/test_no_type_errors.py`, which runs with every `system/*.py`, replaces it.

`select-tests` runs a skill's `python/` project as part of the skill's own suite: a change that reaches it (a library it depends on, a `uv.lock` upgrade) selects `.agents/skills/<name>` once, instead of the skill and its `python/` directory as two runs of the same tests.
