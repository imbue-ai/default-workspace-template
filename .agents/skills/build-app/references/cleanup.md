# Removing an app

1. If the app is still framed as a mock, take the frame down first, which closes
   the mock window and removes the `<name>-sketch` app:
   `uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py down --app <name>`.
2. `python3 system/scripts/forward_port.py --name <name> --remove` (drops the
   entry from `data/.state/apps.toml`).
3. Stop the program and delete its drop-in, then reconcile:

   ```bash
   supervisorctl stop <name>
   rm system/supervisord.conf.d/<name>.conf
   supervisorctl reread && supervisorctl update
   ```

   (See `.agents/shared/references/service-processes.md` for the
   mechanics.)
4. If you scaffolded a lib, also: `rm -rf system/apps/<package>/`. The app is a
   standalone project with its own `uv.lock` and venv, so nothing shared needs
   editing: the root `pyproject.toml` and `uv.lock` never named it, and the
   app's venv directory beside the uv cache is deleted by the next run of the
   venv-link helper (at boot, worktree setup or an update) once its `.venv`
   link is gone.
