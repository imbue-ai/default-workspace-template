# Migrating a workspace's own Python to the packaged-scripts layout

The release that moved every skill's Python into a package in the skill's
`python/` project (and moved one-off commands to `uv run --no-sync`) changes rules the workspace's own
creations were written under. Built-in code arrives already converted; this
reference is for what the *user's* skills, services and docs need when the
update's checks fail on them, and for deciding which skills are the user's. The
rules themselves are in `.agents/shared/references/running-python.md` and
`.agents/shared/references/spec-summary.md` ("Packaging").

## Which skills are the workspace's own

The checks hold a skill to the workspace's rules only when its SKILL.md
frontmatter carries `metadata.crystallized: true` (made in this workspace) or
`metadata.author: imbue` (built in). A skill with neither is taken for a third
party's: it is kept as it came, and the checks leave it out. A skill this
workspace's agents wrote may carry neither, so decide for each skill

```bash
uv run --no-sync .agents/shared/scripts/agents_shared/skill_ownership.py unmarked
```

prints whether it was made here:

- **Its history**: `git log --format='%h %ad %s' --date=short -- .agents/skills/<name>`.
  A skill written here usually arrives in a commit saying it was made, and
  changes in later ones; one installed or copied in arrives whole, often in a
  commit naming where it came from.
- **Its files**: a `skills-lock.json` entry naming it, a LICENSE, a
  `metadata.author` naming someone else, or a README pointing at an upstream
  repository mean it came from elsewhere.
- **The chat that added it**, when those leave it open: find it by the adding
  commit's time with the `find-transcripts` skill, and read whether the agent
  wrote the skill or fetched it.

Made here: add `crystallized: true` under `metadata:` in its frontmatter (the
checks then cover it, and any failure on it is fixed below). Came from
elsewhere: leave it untouched. Still open after all three: mark it. A third
party's skill marked by mistake can at worst fail a check you see, while one of
the user's left unmarked goes unchecked without a sign.

## What fails, and where to fix it

How each failure shows up during the update:

| Symptom | Section |
|---|---|
| `system/test_entry_point_import_cost.py` cannot import a user skill's script (a PEP 723 dependency missing from the root venv) | [A user skill with scripts](#a-user-skill-with-scripts) |
| `uv lock --check` / `uv sync` fails: "Workspace member `.agents/skills/<name>/python` is missing a `pyproject.toml`", or `system/test_meta_ratchets.py::test_every_skill_python_dir_has_a_pyproject` fails | [A packaged skill](#a-packaged-skill), step 1 |
| `system/test_python_launch_rules.py::test_no_supervisord_program_runs_under_uv_run` names a user drop-in | [A user service](#a-user-service) |
| `test_no_plain_uv_run_in_agent_facing_files`, `test_python3_invocations_name_bare_entry_points` or `test_uv_run_invocations_name_scripts_that_exist` names a user file | [Commands in the user's docs](#commands-in-the-users-docs) |
| `system/test_entry_point_import_cost.py` names a user skill's entry | [A packaged skill](#a-packaged-skill), last step |
| `test_prevent_sys_path_edits` or `test_prevent_loading_modules_by_file_path` fails and lists a user file | [A packaged skill](#a-packaged-skill), step 2 |

## A user skill with scripts

First decide which layout the skill takes. Its Python's imports decide, the
same rule `running-python.md` states for built-in code:

- **One script, standard library only** (its PEP 723 header lists no
  dependencies, or it has none, and it imports nothing else of the skill's or the
  workspace's): make it a bare skill, below.
- **Anything else** (a dependency, or more than one module besides a test): the
  `python/` project, the numbered steps after that.

### A bare skill

1. Rename the script to `scripts/<name_with_underscores>.py` (`<name>` with
   hyphens turned to underscores; a `run.py` becomes this) and its test, if it
   has one, to `scripts/<name_with_underscores>_test.py`, importing the script
   by that name. Delete the PEP 723 header and any `sys.path` edit.
2. Point the skill's SKILL.md, its routines' commands and every other caller at
   `python3 .agents/skills/<name>/scripts/<name_with_underscores>.py`.
3. `uv run --no-sync .agents/shared/scripts/agents_shared/validate_skill.py .agents/skills/<name>`
   must print `ok`, and the skill's test must pass
   (`uv run --no-sync pytest .agents/skills/<name>/scripts`).

### A packaged skill

For a skill with Python in `.agents/skills/<name>/scripts/` (package name
`<name_with_underscores>_skill`: `<name>` with hyphens turned to underscores,
plus `_skill`; a `scripts/` dir of shell scripts only needs nothing):

1. Create `.agents/skills/<name>/python/pyproject.toml` in the shape
   `spec-summary.md` ("Packaging") shows: project `<name>-skill`, hatchling,
   `packages = ["<name_with_underscores>_skill"]`. Its `dependencies` are the
   union of the scripts' PEP 723 `# /// script` headers; delete those headers.
2. Move every module into `python/<name_with_underscores>_skill/` (with an empty
   `__init__.py`), tests included. A script keeps its own
   `if __name__ == "__main__":` block and now runs by its new path: point the
   skill's SKILL.md, its routines' commands and every other caller in the
   workspace at that path. Only where a caller cannot be changed, leave a thin
   entry file at the old path in `scripts/` that imports the module from the
   package and calls it under `if __name__ == "__main__":`, with a docstring
   saying what still runs that path.
   Change sibling imports (`import helpers`) to package imports
   (`from <name_with_underscores>_skill import helpers`), and delete any `sys.path` edit that
   existed to make them work. A test that loads its module from a file
   (`importlib.util.spec_from_file_location`, `runpy.run_path`) imports it from
   the package instead. A path computed from `__file__` in a moved module is now
   one directory deeper.
3. `uv lock`, then `uv sync --all-packages`. A dependency that cannot
   co-resolve with the workspace fails here; pick a compatible version (the
   skill shares the workspace's lock now), and say so in the report.
4. `uv run --no-sync .agents/shared/scripts/agents_shared/validate_skill.py .agents/skills/<name>`
   must print `ok`, and the skill's tests must pass by path.
5. If the import-cost test names the entry, move the heavy import into the
   function that uses it; code whose signatures name that library's types goes
   in a module of its own, imported inside the subcommands that need it.

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
- a path to a built-in script that now runs from its module becomes the
  module's path, keeping the interpreter the script's tier calls for: a skill's
  `scripts/<name>.py` is `python/<skill_name>_skill/<name>.py` (for example
  `python3 .agents/skills/notify-user/python/notify_user_skill/notify_user.py`;
  `notify_user.py`, `request_secret.py`, `serve_isolated_instance.py`,
  `resolve_template_base.py` and `preview_wrapper_server.py` are bare and run
  under `python3`, the rest under `uv run --no-sync`), a
  shared one is under `.agents/shared/scripts/agents_shared/`, and
  `system/scripts/run_in_background.py`, `tool_env.py` and
  `list_mngr_plugins.py` are under `system/scripts/workspace_bare_scripts/`;
- `from claude_p import ...` run from the root venv becomes
  `from use_ai_integration_skill.claude_p import ...` (a copy of `claude_p.py`
  inside the user's own app is theirs and stays as it is).
