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
3. If you scaffolded a lib, also: `uv tool uninstall <name>` (the app's own
   tool environment), `rm -rf system/apps/<package>/`, and
   `uv sync --all-packages` so the root lockfile forgets the member (the
   scaffold never edits the root `pyproject.toml`; the `system/apps/*` glob
   picked the package up).
