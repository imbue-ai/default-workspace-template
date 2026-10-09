This release turns the workspace's scripts into packages. An existing workspace's own skills, services and docs are migrated during the update by following `.agents/skills/update-self/references/python-packaging-migration.md`.

- `system/scripts` is one uv project with two packages. `workspace_bare_scripts` runs under the system `python3` with no venv and imports only the standard library. `workspace_scripts` (the changelog gate, the docs viewer, the auth migration, the view refresh and the app smoketest) runs from the root venv.

- Every existing script path stays as a thin entry file that imports its package. Only the five hook entry files that reach the `tk_command_parsing` or `oom_priority` source tree still edit `sys.path`.

- The root workspace adds the `system/scripts`, `.agents/shared/scripts` and `.agents/skills/*/scripts` members, so skill dependencies resolve in the one `uv.lock`.

- New root checks:
  - `system/test_entry_point_import_cost.py` fails an entry point that loads pydantic, loguru, click, tenacity, httpx or asyncio at import without declaring it.
  - `system/test_external_entry_paths.py` runs every path that Imbue Studio, CI and older update-self releases call by path, with no site-packages.
  - `system/test_python_launch_rules.py` fails a `python3 <path>.py` that names a venv-tier or missing script, and any `sys.path` edit outside the declared hook entry files.
  - `system/test_meta_ratchets.py` requires a `pyproject.toml` in every skill `scripts/` dir. Without one, every `uv` command in the workspace fails.
