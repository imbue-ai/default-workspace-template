# Removing an app

1. `python3 system/scripts/forward_port.py --name <name> --remove` (drops the
   entry from `data/.state/apps.toml`).
2. Stop the program and remove its block from `system/supervisord.conf`, then
   reconcile:

   ```bash
   supervisorctl stop <name>
   # delete the [program:<name>] block from system/supervisord.conf
   supervisorctl reread && supervisorctl update
   ```

   (See `.agents/shared/references/service-processes.md` for the
   mechanics.)
3. If you scaffolded a lib, also: `rm -rf system/apps/<package>/` and revert
   the matching diff in the root `pyproject.toml` (drop from
   `[project].dependencies`, `[tool.uv.workspace].members`, and
   `[tool.uv.sources]`).
