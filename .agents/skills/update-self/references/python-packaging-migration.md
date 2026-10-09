# Migrating a workspace's own Python to the packaged-scripts layout

The release that turned every skill's `scripts/` into a package (and moved
one-off commands to `uv run --no-sync`) changes rules the workspace's own
creations were written under. Built-in code arrives already converted; this
reference is for what the *user's* skills, services and docs need when the
update's checks fail on them. The rules themselves are in
`.agents/shared/references/running-python.md` and
`.agents/shared/references/spec-summary.md` ("Packaging").

How each shows up during the update:

| Symptom | Section |
|---|---|
| `uv lock --check` / `uv sync` fails: "Workspace member `.agents/skills/<name>/scripts` is missing a `pyproject.toml`" | [A user skill with scripts](#a-user-skill-with-scripts) |
| `system/test_meta_ratchets.py::test_every_skill_scripts_dir_has_a_pyproject` fails | [A user skill with scripts](#a-user-skill-with-scripts) |
| `system/test_python_launch_rules.py::test_no_supervisord_program_runs_under_uv_run` names a user drop-in | [A user service](#a-user-service) |
| `test_no_plain_uv_run_in_agent_facing_files` or `test_python3_invocations_name_bare_entry_points` names a user file | [Commands in the user's docs](#commands-in-the-users-docs) |
| `system/test_entry_point_import_cost.py` names a user skill's entry | [A user skill with scripts](#a-user-skill-with-scripts), last step |
| `test_only_the_declared_bare_stubs_edit_sys_path` names a user file | [A user skill with scripts](#a-user-skill-with-scripts) |

## A user skill with scripts

For `.agents/skills/<name>/scripts/` (package name `<name>` with hyphens turned
to underscores, plus `_skill`):

1. Create `scripts/pyproject.toml` in the shape `spec-summary.md` ("Packaging")
   shows: project `<name>-skill`, hatchling, `packages = ["<name>_skill"]`. Its
   `dependencies` are the union of the scripts' PEP 723 `# /// script` headers;
   delete those headers.
2. Move every module into `scripts/<name>_skill/` (with an empty
   `__init__.py`), tests included. Leave a thin entry file at each path the
   skill's SKILL.md, cron entries or program lines run, importing its module
   from the package and calling it under `if __name__ == "__main__":`. Change
   sibling imports (`import helpers`) to package imports
   (`from <name>_skill import helpers`), and delete any `sys.path` edit that
   existed to make them work. A path computed from `__file__` in a moved module
   is now one directory deeper.
3. A `scripts/` dir with only shell scripts gets a `pyproject.toml` with a
   `[project]` table (`name = "<name>-skill"`, `version`) and
   `[tool.uv] package = false`, and nothing else changes.
4. `uv lock`, then `uv sync --all-packages`. A dependency that cannot
   co-resolve with the workspace fails here; pick a compatible version (the
   skill shares the workspace's lock now), and say so in the report.
5. `uv run --no-sync .agents/shared/scripts/validate_skill.py .agents/skills/<name>`
   must print `ok`, and the skill's tests must pass by path.
6. If the import-cost test names the entry, move the heavy import into the
   function that uses it, or declare it as the test's message says.

## A user service

A `system/supervisord.conf.d/<name>.conf` whose `command=` runs `uv run <name>`
(often behind the `oom_tag_service.py` prefix) becomes:

```ini
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "uv sync --all-packages --frozen && exec .venv/bin/<name>"
```

keeping whatever OOM tag and other `bash -c` steps it had. After the apply the
program picks it up on its next restart.

## Commands in the user's docs

In the user's own SKILL.md files, references, prompts and scripts:

- a plain `uv run X` becomes `uv run --no-sync X` (keep `uv run --no-project`
  as it is);
- `python3 <path>.py` for a script that is not one of the bare entry points the
  check accepts becomes `uv run --no-sync <path>.py`;
- `from claude_p import ...` run from the root venv becomes
  `from use_ai_integration_skill.claude_p import ...` (a copy of `claude_p.py`
  inside the user's own app is theirs and stays as it is).
