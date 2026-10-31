# Removing an app

1. `python3 system/scripts/forward_port.py --name <name> --remove` (drops the
   entry from `data/.state/apps.toml`).
2. Stop the program and delete its drop-in, then reconcile:

   ```bash
   supervisorctl stop <name>
   rm system/supervisord.conf.d/<name>.conf
   supervisorctl reread && supervisorctl update
   ```

   (See `.agents/shared/references/service-processes.md` for the
   mechanics.)
3. If you scaffolded a lib, also: `rm -rf system/apps/<package>/`. The app is a
   standalone project with its own `uv.lock` and venv, so nothing shared needs
   editing: the root `pyproject.toml` and `uv.lock` never named it, and the
   app's venv directory beside the uv cache is deleted by the next run of the
   venv-link helper (at boot, worktree setup or an update) once its `.venv`
   link is gone.
